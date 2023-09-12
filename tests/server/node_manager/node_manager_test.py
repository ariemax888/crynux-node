import os
import shutil
from io import BytesIO
from typing import List

import pytest
from anyio import create_task_group, sleep
from eth_account import Account
from PIL import Image
from web3 import Web3

from h_server import models
from h_server.config import Config, TxOption
from h_server.contracts import Contracts
from h_server.event_queue import EventQueue, MemoryEventQueue
from h_server.models.task import PoseConfig, TaskConfig
from h_server.node_manager import NodeManager
from h_server.node_manager.state_cache import MemoryNodeStateCache
from h_server.relay import MockRelay, Relay
from h_server.task import InferenceTaskRunner, MemoryTaskStateCache, TaskSystem
from h_server.task.state_cache import TaskStateCache
from h_server.utils import get_task_data_hash, get_task_hash
from h_server.watcher import EventWatcher, MemoryBlockNumberCache


@pytest.fixture(scope="module")
def tx_option():
    return {}


@pytest.fixture(scope="module")
def privkeys():
    return [
        "0xa627246a109551432ac5db6535566af34fdddfaa11df17b8afd53eb987e209a2",
        "0xb171f296622b98cbdc08dcdcb0696f738c3a22d9d367c657117cd3c8d0b71d42",
        "0x8fb2fc9862b93b5b75cda8202f583711201e4cba5459eefe442b8c5dcc4bdab9",
    ]


@pytest.fixture(scope="module")
async def root_contracts(tx_option, privkeys):
    from web3.providers.eth_tester import AsyncEthereumTesterProvider

    provider = AsyncEthereumTesterProvider()
    c0 = Contracts(provider=provider, default_account_index=0)

    await c0.init(option=tx_option)

    await c0.node_contract.update_task_contract_address(
        c0.task_contract.address, option=tx_option
    )

    for privkey in privkeys:
        provider.ethereum_tester.add_account(privkey)
        account = Account.from_key(privkey)
        amount = Web3.to_wei(1000, "ether")
        await c0.transfer(account.address, amount, option=tx_option)

    return c0


@pytest.fixture(scope="module")
def config():
    test_config = Config.model_validate(
        {
            "log": {"dir": "logs", "level": "INFO"},
            "ethereum": {
                "privkey": "",
                "provider": "",
                "contract": {"token": "", "node": "", "task": ""},
            },
            "task_dir": "task",
            "db": "",
            "relay_url": "",
            "celery": {"broker": "", "backend": ""},
            "distributed": False,
            "task_config": {
                "data_dir": "build/data/workspace",
                "pretrained_models_dir": "build/data/pretrained-models",
                "controlnet_models_dir": "build/data/controlnet",
                "training_logs_dir": "build/data/training-logs",
                "inference_logs_dir": "build/data/inference-logs",
                "script_dir": "remote-lora-scripts",
                "result_url": "",
            },
        }
    )
    return test_config


@pytest.fixture(scope="module")
async def node_contracts(
    root_contracts: Contracts, tx_option: TxOption, privkeys: List[str]
):
    token_contract_address = root_contracts.token_contract.address
    node_contract_address = root_contracts.node_contract.address
    task_contract_address = root_contracts.task_contract.address

    cs = []
    for privkey in privkeys:
        contracts = Contracts(provider=root_contracts.provider, privkey=privkey)
        await contracts.init(
            token_contract_address, node_contract_address, task_contract_address
        )
        amount = Web3.to_wei(1000, "ether")
        if (await contracts.token_contract.balance_of(contracts.account)) < amount:
            await root_contracts.token_contract.transfer(
                contracts.account, amount, option=tx_option
            )
        task_amount = Web3.to_wei(400, "ether")
        if (
            await contracts.token_contract.allowance(task_contract_address)
        ) < task_amount:
            await contracts.token_contract.approve(
                task_contract_address, task_amount, option=tx_option
            )
        node_amount = Web3.to_wei(400, "ether")
        if (
            await contracts.token_contract.allowance(node_contract_address)
        ) < node_amount:
            await contracts.token_contract.approve(
                node_contract_address, node_amount, option=tx_option
            )

        cs.append(contracts)
    return cs


@pytest.fixture(scope="module")
def relay():
    return MockRelay()


@pytest.fixture(scope="module")
async def node_managers(
    privkeys: List[str], node_contracts: List[Contracts], relay: Relay, config: Config
):
    managers = []
    new_data_dirs = []

    for i, (privkey, contracts) in enumerate(zip(privkeys, node_contracts)):
        queue = MemoryEventQueue()

        watcher = EventWatcher.from_contracts(contracts)
        block_number_cache = MemoryBlockNumberCache()
        watcher.set_blocknumber_cache(block_number_cache)

        def make_callback(queue):
            async def _push_event(event_data):
                event = models.load_event_from_contracts(event_data)
                await queue.put(event)

            return _push_event

        watcher.watch_event(
            "task",
            "TaskCreated",
            callback=make_callback(queue),
            filter_args={"selectedNode": contracts.account},
        )

        task_state_cache = MemoryTaskStateCache()
        system = TaskSystem(
            task_state_cache,
            queue=queue,
            distributed=config.distributed,
            task_name="mock_lora_inference",
        )

        assert config.task_config is not None
        local_config = config.task_config.model_copy()
        data_dir = f"build/data/workspace{i}"
        if not os.path.exists(data_dir):
            shutil.copytree(local_config.data_dir, data_dir)
        local_config.data_dir = data_dir
        new_data_dirs.append(data_dir)

        def make_runner_cls(contracts, relay, watcher, local_config):
            class _InferenceTaskRunner(InferenceTaskRunner):
                def __init__(
                    self,
                    task_id: int,
                    state_cache: TaskStateCache,
                    queue: EventQueue,
                    task_name: str,
                    distributed: bool,
                ) -> None:
                    super().__init__(
                        task_id,
                        state_cache,
                        queue,
                        task_name,
                        distributed,
                        contracts,
                        relay,
                        watcher,
                        local_config,
                    )

            return _InferenceTaskRunner

        system.set_runner_cls(make_runner_cls(contracts, relay, watcher, local_config))

        manager = NodeManager(
            config=config,
            node_state_cache_cls=MemoryNodeStateCache,
            privkey=privkey,
            event_queue=queue,
            contracts=contracts,
            relay=relay,
            watcher=watcher,
            task_system=system,
        )
        managers.append(manager)

    try:
        yield managers
    finally:
        for data_dir in new_data_dirs:
            if os.path.exists(data_dir):
                shutil.rmtree(data_dir)


async def test_node_manager(
    node_managers: List[NodeManager],
    node_contracts: List[Contracts],
    relay: Relay,
    tx_option,
):
    n1, n2, n3 = node_managers
    c1, c2, c3 = node_contracts

    async with create_task_group() as tg:
        assert (await n1.get_state()).status == models.NodeStatus.Init
        assert (await n2.get_state()).status == models.NodeStatus.Init
        assert (await n3.get_state()).status == models.NodeStatus.Init

        tg.start_soon(n1.run)
        tg.start_soon(n2.run)
        tg.start_soon(n3.run)

        while (await n1.get_state()).status == models.NodeStatus.Init:
            await sleep(0.1)
        while (await n2.get_state()).status == models.NodeStatus.Init:
            await sleep(0.1)
        while (await n3.get_state()).status == models.NodeStatus.Init:
            await sleep(0.1)

        await n1.start()
        await n2.start()
        await n3.start()

        assert (await n1.get_state()).status == models.NodeStatus.Running
        assert (await n2.get_state()).status == models.NodeStatus.Running
        assert (await n3.get_state()).status == models.NodeStatus.Running

        task = models.RelayTaskInput(
            task_id=1,
            base_model="stable-diffusion-v1-5-pruned",
            prompt="a mame_cat lying under the window, in anime sketch style, red lips, blush, black eyes, dashed outline, brown pencil outline",
            lora_model="f4fab20c-4694-430e-8937-22cdb713da9",
            task_config=TaskConfig(
                image_width=512,
                image_height=512,
                lora_weight=100,
                num_images=1,
                seed=255728798,
                steps=40,
            ),
            pose=PoseConfig(data_url="", pose_weight=100, preprocess=False),
        )

        task_hash = get_task_hash(task.task_config)
        data_hash = get_task_data_hash(
            base_model=task.base_model,
            lora_model=task.lora_model,
            prompt=task.prompt,
            pose=task.pose,
        )
        await relay.create_task(task=task)
        await c1.task_contract.create_task(
            task_hash=task_hash, data_hash=data_hash, option=tx_option
        )

        with BytesIO() as dst:
            await relay.get_result(task_id=1, image_num=0, dst=dst)
            dst.seek(0)
            img = Image.open(dst)
            assert img.width == 512
            assert img.height == 512

        await n1.pause()
        await n2.pause()
        await n3.pause()

        assert (await n1.get_state()).status == models.NodeStatus.Paused
        assert (await n2.get_state()).status == models.NodeStatus.Paused
        assert (await n3.get_state()).status == models.NodeStatus.Paused

        await n1.resume()
        await n2.resume()
        await n3.resume()

        assert (await n1.get_state()).status == models.NodeStatus.Running
        assert (await n2.get_state()).status == models.NodeStatus.Running
        assert (await n3.get_state()).status == models.NodeStatus.Running

        await n1.stop()
        await n2.stop()
        await n3.stop()

        assert (await n1.get_state()).status == models.NodeStatus.Stopped
        assert (await n2.get_state()).status == models.NodeStatus.Stopped
        assert (await n3.get_state()).status == models.NodeStatus.Stopped

        await n1.finish()
        await n2.finish()
        await n3.finish()
        tg.cancel_scope.cancel()
