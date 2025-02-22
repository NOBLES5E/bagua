import logging
import torch
import torch.distributed as dist
import torch.distributed.distributed_c10d as c10d
import bagua_core as B

import bagua.torch_api
from .env import (
    get_world_size,
    get_rank,
    get_local_rank,
    get_local_size,
    get_autotune_server_addr,
)
from ..service.autotune_service import AutotuneClient
from .exceptions import RepeatedInitializationError
from .utils import flatten, unflatten, to_bagua_datatype
from ..bagua_define import BaguaHyperparameter

_global_state = None


def _get_global_state():
    global _global_state
    return _global_state


def is_initialized():
    """
    Checking if bagua global communication state has been initialized
    """
    global _global_state
    return _global_state is not None


def init_process_group(init_method: str = "dist://", device_id=None):
    if is_initialized():
        raise RepeatedInitializationError()

    metas = init_method.split("://")
    if metas[0] not in ["dist", "file"]:
        raise ValueError("Illegal init method: {}".format(init_method))

    # create store
    if metas[0] == "file":
        store = dist.FileStore(metas[1], get_world_size())  # type: ignore
    else:
        # init default group first
        if not dist.is_initialized():
            torch.distributed.init_process_group(backend="nccl", init_method="env://")

        store = c10d._get_default_store()

    global _global_state
    _global_state = BaguaGlobalState(store, device_id=device_id)


class BaguaGlobalState(object):
    def __init__(self, store=None, device_id=None):
        if device_id is None:
            device_id = get_local_rank()
        self.backend = B.BaguaCommBackendPy(100, device_id=device_id)
        self.stream = torch.cuda.Stream(priority=-1)
        self.store = store
        self.hyperparameters = BaguaHyperparameter()
        self.hyperparameters_service_client = AutotuneClient(get_autotune_server_addr())
        self.internode_communicator = init_bagua_inter_communicator(
            stream=self.stream, leader_rank=0, store=self.store, device_id=device_id
        )
        self.intranode_communicator = init_bagua_intra_communicator(
            stream=self.stream, store=self.store, device_id=device_id
        )
        self.global_communicator = init_bagua_communicator(
            stream=self.stream, store=self.store, device_id=device_id
        )

    def get_internode_communicator(self):
        return self.internode_communicator

    def get_intranode_communicator(self):
        return self.intranode_communicator

    def get_global_communicator(self):
        return self.global_communicator

    def get_backend(self):
        return self.backend


def get_bagua_hyperparameters():
    return _global_state.hyperparameters


def get_hyperparameters_service_client():
    return _global_state.hyperparameters_service_client


def gen_nccl_unique_id(comm_type: str, root=0, store=None):
    key = f"{comm_type}-{root}-unique_id"

    if store is None:
        store = c10d._get_default_store()

    if get_rank() == root:
        idstr = B.BaguaSingleCommunicatorPy.generate_nccl_unique_id_str()
        store.set(key, idstr)
    else:
        idstr = store.get(key)
        idstr = str(idstr, encoding="utf-8")

    return idstr


def init_bagua_inter_communicator(stream, leader_rank=0, store=None, device_id=None):
    if device_id is None:
        device_id = get_local_rank()
    nccl_unique_id = gen_nccl_unique_id(
        "bagua_inter_comm", root=leader_rank, store=store
    )

    if get_rank() % get_local_size() != leader_rank:
        return None

    comm = B.BaguaSingleCommunicatorPy(
        rank=get_rank() // get_local_size(),
        nranks=get_world_size() // get_local_size(),
        device_id=device_id,
        stream_ptr=stream.cuda_stream,
        nccl_unique_id_str=nccl_unique_id,
    )
    comm.cuda_stream = stream
    logging.debug(
        f"init bagua internode communicator ok, global rank: {dist.get_rank()} rank: {comm.rank()}"
    )
    return comm


def init_bagua_intra_communicator(stream, store=None, device_id=None):
    if device_id is None:
        device_id = get_local_rank()
    nccl_unique_id = gen_nccl_unique_id(
        "bagua_intra_comm",
        root=get_rank() // get_local_size() * get_local_size(),
        store=store,
    )

    comm = B.BaguaSingleCommunicatorPy(
        rank=get_rank() % get_local_size(),
        nranks=get_local_size(),
        device_id=device_id,
        stream_ptr=stream.cuda_stream,
        nccl_unique_id_str=nccl_unique_id,
    )
    comm.cuda_stream = stream
    logging.debug(
        f"init bagua intranode communicator ok, global rank: {dist.get_rank()} rank: {comm.rank()}"
    )
    return comm


def init_bagua_communicator(stream, store=None, device_id=None):
    if device_id is None:
        device_id = get_local_rank()
    nccl_unique_id = gen_nccl_unique_id("bagua_global_comm", store=store)

    comm = B.BaguaSingleCommunicatorPy(
        rank=get_rank(),
        nranks=get_world_size(),
        device_id=device_id,
        stream_ptr=stream.cuda_stream,
        nccl_unique_id_str=nccl_unique_id,
    )
    comm.cuda_stream = stream
    logging.debug(
        f"init bagua global communicator ok, global rank: {dist.get_rank()} rank: {comm.rank()}"
    )
    return comm


def broadcast_coalesced(tensors, root=0, comm: B.BaguaSingleCommunicatorPy = None):
    for tensor in tensors:
        assert tensor.device != torch.device(
            "cpu"
        ), "input tensors must be CUDA and dense"

    if comm is None:
        comm = _get_global_state().get_global_communicator()

    event = torch.cuda.current_stream().record_event()
    comm.cuda_stream.wait_event(event)

    with torch.cuda.stream(comm.cuda_stream):
        coalesced = flatten(tensors)
        b_coalesced = B.BaguaTensorPy(
            ptr=coalesced.data_ptr(),
            num_elem=coalesced.numel(),
            num_elem_allocated=coalesced.numel(),
            dtype=to_bagua_datatype(coalesced.dtype),
            device_id=coalesced.device.index,
        )
        comm.broadcast(b_coalesced, root)

        for buf, synced in zip(tensors, unflatten(coalesced, tensors)):
            buf.copy_(synced)

    torch.cuda.synchronize()


def broadcast(tensor, root=0, comm: B.BaguaSingleCommunicatorPy = None):
    """
    Broadcasts the tensor to the whole communicator.

    `tensor` must have the same number of elements in all processes participating in the collective.

    Arguments:
    * `tensor`(_torch.Tensor_) - Data to be sent if `root` is the rank of current process, and tensor to be used to save received data otherwise.
    * `root`(_int_) - Source rank.
    * `comm`(_B.BaguaSingleCommunicatorPy_) - The bagua communicator to work on. If None, the global bagua communicator will be used.

    Note: To broadcast a list of tensors, use `broadcast_coalesced` instead.
    """

    assert tensor.device != torch.device("cpu"), "input tensor must be CUDA and dense"

    if comm is None:
        comm = _get_global_state().get_global_communicator()

    event = torch.cuda.current_stream().record_event()
    comm.cuda_stream.wait_event(event)

    with torch.cuda.stream(comm.cuda_stream):
        b_tensor = B.BaguaTensorPy(
            ptr=tensor.data_ptr(),
            num_elem=tensor.numel(),
            num_elem_allocated=tensor.numel(),
            dtype=to_bagua_datatype(tensor.dtype),
            device_id=tensor.device.index,
        )
        comm.broadcast(b_tensor, root)

    torch.cuda.synchronize()


def allreduce_coalesced(
    tensors,
    comm: B.BaguaSingleCommunicatorPy = None,
    average: bool = True,
):
    for tensor in tensors:
        assert tensor.device != torch.device(
            "cpu"
        ), "input tensors must be CUDA and dense"

    if comm is None:
        comm = _get_global_state().get_global_communicator()

    event = torch.cuda.current_stream().record_event()
    comm.cuda_stream.wait_event(event)

    with torch.cuda.stream(comm.cuda_stream):
        coalesced = flatten(tensors)
        b_coalesced = B.BaguaTensorPy(
            ptr=coalesced.data_ptr(),
            num_elem=coalesced.numel(),
            num_elem_allocated=coalesced.numel(),
            dtype=to_bagua_datatype(coalesced.dtype),
            device_id=coalesced.device.index,
        )
        comm.allreduce(b_coalesced)

        if average:
            coalesced /= comm.nranks()

        for buf, synced in zip(tensors, unflatten(coalesced, tensors)):
            buf.copy_(synced)

    torch.cuda.synchronize()


def allreduce(
    tensor,
    comm: B.BaguaSingleCommunicatorPy = None,
    average: bool = True,
):
    """
    Reduces the tensor data across all machines in such a way that all get the final result.
    After the call tensor is going to be bitwise identical in all processes.

    Arguments:
    * `tensor`(_torch.Tensor_) - Input and output of the collective. The function operates in-place.
    * `comm`(_B.BaguaSingleCommunicatorPy_) - The bagua communicator to work on. If None, the global bagua communicator will be used.
    * `average`(_bool_) - Average the reduced tensor or not.

    Note: To allreduce a list of tensors, use `allreduce_coalesced` instead.
    """

    assert tensor.device != torch.device("cpu"), "input tensor must be CUDA and dense"

    if comm is None:
        comm = _get_global_state().get_global_communicator()

    event = torch.cuda.current_stream().record_event()
    comm.cuda_stream.wait_event(event)

    with torch.cuda.stream(comm.cuda_stream):
        b_tensor = B.BaguaTensorPy(
            ptr=tensor.data_ptr(),
            num_elem=tensor.numel(),
            num_elem_allocated=tensor.numel(),
            dtype=to_bagua_datatype(tensor.dtype),
            device_id=tensor.device.index,
        )
        comm.allreduce(b_tensor)

        if average:
            tensor /= comm.nranks()

    torch.cuda.synchronize()
