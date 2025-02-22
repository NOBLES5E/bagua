import os


def get_world_size():
    """
    Returns the number of processes in the current process group

    Returns:
        The world size of the process group

    """
    return int(os.environ.get("WORLD_SIZE", 1))


def get_rank():
    """
    Returns the rank of current process group

    Rank is a unique identifier assigned to each process within a distributed
    process group. They are always consecutive integers ranging from 0 to
    ``world_size``.

    Returns:
        The rank of the process group

    """
    return int(os.environ.get("RANK", 0))


def get_local_rank():
    """
    Returns the rank of current node

    Rank is a unique identifier assigned to each process within a node.
    They are always consecutive integers ranging from 0 to ``local_size``.

    Returns:
        The local rank of the node

    """
    return int(os.environ.get("LOCAL_RANK", 0))


def get_local_size():
    """
    Returns the number of processes in the node

    Returns:
        The local size of the node

    """
    return int(os.environ.get("LOCAL_SIZE", 1))


def get_autotune_server_addr():
    return os.environ.get("AUTO_TUNE_SERVER_ADDR")


def is_report_metrics_switch_on():
    return int(os.environ.get("BAGUA_REPORT_METRICS", 0)) == 1


def get_autotune_level():
    return int(os.environ.get("BAGUA_AUTOTUNE", 0))
