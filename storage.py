#!/usr/bin/env python

import argparse
import configparser
import logging
import random
from dataclasses import dataclass
from random import expovariate
from typing import Optional, List

# the humanfriendly library (https://humanfriendly.readthedocs.io/en/latest/) lets us pass parameters in human-readable
# format (e.g., "500 KiB" or "5 days"). You can safely remove this if you don't want to install it on your system, but
# then you'll need to handle sizes in bytes and time spans in seconds--or write your own alternative.
# It should be trivial to install (e.g., apt install python3-humanfriendly or conda/pip install humanfriendly).
from humanfriendly import format_timespan, parse_size, parse_timespan

from discrete_event_sim import Simulation, Event


def exp_rv(mean):
    """Return an exponential random variable with the given mean."""
    return expovariate(1 / mean)


class DataLost(Exception):
    """Not enough redundancy in the system, data is lost. We raise this exception to stop the simulation."""
    pass


class Backup(Simulation):
    """Backup simulation.
    """

    # type annotations for `Node` are strings here to allow a forward declaration:
    # https://stackoverflow.com/questions/36193540/self-reference-or-forward-reference-of-type-annotations-in-python
    def __init__(self, nodes: List['Node']):
        super().__init__()  # call the __init__ method of parent class
        self.nodes = nodes
        # to collect data for summary
        self.initial_data_objects = len(nodes)  # предполагаем, что только ноды с данными
        self.successful_recoveries = 0
        self.data_loss_events = 0
        self.total_recovery_attempts = 0
        self.total_recovery_time = 0.0  # суммируем по всем успешным восстановлению
        self.recovery_start_times = {}  # по node.id храним время начала восстановления

        # we add to the event queue the first event of each node going online and of failing
        for node in nodes:
            # we plan to make the node go online at its arrival time
            self.schedule(node.arrival_time, Online(node))
            # we plan to make the node fail at its arrival time + average_lifetime
            self.schedule(node.arrival_time + exp_rv(node.average_lifetime), Fail(node))

    def schedule_transfer(self, uploader: 'Node', downloader: 'Node', block_id: int, restore: bool):
        """Helper function called by `Node.schedule_next_upload` and `Node.schedule_next_download`.

        If `restore` is true, we are restoring a block owned by the downloader, otherwise, we are saving one owned by
        the uploader.
        """
        # define the block size by its owner: downloader or uploader
        block_size = downloader.block_size if restore else uploader.block_size

        # check that the uploader and downloader don't upload or download anything
        assert uploader.current_upload is None
        assert downloader.current_download is None

        # define the speed: the slowest between the two nodes
        speed = min(uploader.upload_speed, downloader.download_speed)  # we take the slowest between the two
        # define the time of transfer
        delay = block_size / speed

        # restore:
        # True if the downloader is restoring a block from the uploader
        # False if the uploader is backing up a block to the downloader
        if restore:
            # set an event to restore the block
            event = BlockRestoreComplete(uploader, downloader, block_id)
        else:
            # set an event to back up the block
            event = BlockBackupComplete(uploader, downloader, block_id)
        # add the event to the event queue
        self.schedule(delay, event)
        # update the status of the uploader and downloader: they are busy now
        downloader.current_download = event
        uploader.current_upload = event


    def summary(self):
        total_data_loss_events = sum(n.total_data_loss_events for n in self.nodes)
        total_data_recovered = sum(n.total_data_recovered for n in self.nodes)
        total_backups = sum(n.total_backups_made for n in self.nodes)
        total_restores = sum(n.total_restores_made for n in self.nodes)

        vulnerable_blocks = 0
        total_blocks = 0
        for n in self.nodes:
            total_blocks += n.n
            for peer in n.backed_up_blocks:
                if peer is not None:
                    # SUM
                    owners = sum(1 for other in self.nodes if n in other.remote_blocks_held)
                    if owners == 1:
                        #if the block is backed up only on a remote node we add it to the vulnerable blocks
                        vulnerable_blocks += 1

        # percent of the restored data
        percent_restored = 100 * total_data_recovered / total_data_loss_events if total_data_loss_events else 100
        # percent of nodes that experienced at least one failure
        percent_nodes_failed = 100 * sum(1 for n in self.nodes if n.total_data_loss_events > 0) / len(self.nodes)

        print("\nSummary of the simulation:")
        print(f"Simulated time: {format_timespan(self.t)}")
        print(f"Total nodes: {len(self.nodes)}")
        print(f"Total data loss events: {total_data_loss_events}")
        print(f"Total data recovery events: {total_data_recovered}")
        print(f"Data recovery success rate: {percent_restored:.2f}%")
        print(f"Nodes that experienced at least one failure: {percent_nodes_failed:.2f}%")
        print(f"Total backups made: {total_backups}")
        print(f"Total restores made: {total_restores}")
        print(f"Vulnerable blocks {vulnerable_blocks} / {total_blocks}")



    def log_info(self, msg):
        """Override method to get human-friendly logging for time."""

        logging.info(f'{format_timespan(self.t)}: {msg}')


@dataclass(eq=False)  # auto initialization from parameters below (won't consider two nodes with same state as equal)
class Node:
    """Class representing the configuration of a given node."""

    # using dataclass is (for our purposes) equivalent to having something like
    # def __init__(self, description, n, k, ...):
    #     self.n = n
    #     self.k = k
    #     ...
    #     self.__post_init__()  # if the method exists

    name: str  # the node's name

    n: int  # number of blocks in which the data is encoded
    k: int  # number of blocks sufficient to recover the whole node's data

    data_size: int  # amount of data to back up (in bytes)
    storage_size: int  # storage space devoted to storing remote data (in bytes)

    upload_speed: float  # node's upload speed, in bytes per second
    download_speed: float  # download speed

    average_uptime: float  # average time spent online
    average_downtime: float  # average time spent offline

    average_lifetime: float  # average time before a crash and data loss
    average_recover_time: float  # average time after a data loss

    arrival_time: float  # time at which the node will come online

    def __post_init__(self):
        """Compute other data dependent on config values and set up initial state."""

        # whether this node is online. All nodes start offline.
        self.online: bool = False

        # whether this node is currently under repairs. All nodes are ok at start.
        self.failed: bool = False

        # size of each block
        self.block_size: int = self.data_size // self.k if self.k > 0 else 0

        # amount of free space for others' data -- note we always leave enough space for our n blocks
        self.free_space: int = self.storage_size - self.block_size * self.n

        assert self.free_space >= 0, "Node without enough space to hold its own data"

        # local_blocks[block_id] is true if we locally have the local block
        # [x] * n is a list with n references to the object x
        self.local_blocks: list[bool] = [True] * self.n

        # backed_up_blocks[block_id] is the peer we're storing that block on, or None if it's not backed up yet;
        # we start with no blocks backed up
        self.backed_up_blocks: list[Optional[Node]] = [None] * self.n

        # (owner -> block_id) mapping for remote blocks stored
        self.remote_blocks_held: dict[Node, int] = {}

        # current uploads and downloads, stored as a reference to the relative TransferComplete event
        self.current_upload: Optional[TransferComplete] = None
        self.current_download: Optional[TransferComplete] = None

        # for Summary()
        self.total_data_loss_events: int = 0  # how many times data was lost Fail.process()
        self.total_data_recovered: int = 0    # how many times data was recovered BlockRestoreComplete.update_block_state()
        self.total_backups_made: int = 0      # how many blocks were backed up BlockBackupComplete.update_block_state()
        self.total_restores_made: int = 0     # how many blocks were restored BlockRestoreComplete.update_block_state()


    def find_block_to_back_up(self): # +TODO
        """Returns the block id of a block that needs backing up, or None if there are none."""

        # find a block that we have locally but not remotely
        # check `enumerate` and `zip` at https://docs.python.org/3/library/functions.html
        for block_id, (held_locally, peer) in enumerate(zip(self.local_blocks, self.backed_up_blocks)):
           # held_locally - our block is on our node # +TODO
           # peer - the block is backed up on a remote node # +TODO
           if held_locally and peer is None:
                return block_id # return the block we need to back up
        return None 

    def schedule_next_upload(self, sim: Backup):
        """Schedule the next upload, if any."""

        assert self.online # we can only upload if we're online

        # we don't want to upload if we're already uploading something
        if self.current_upload is not None:
            return

        #  1. first find if we have a backup that a remote node needs
        for peer, block_id in self.remote_blocks_held.items():
            # peer.online - if the remote peer is online\
            # peer.current_download is None - if the remote peer is not downloading anything
            # not peer.local_blocks[block_id] - if the remote peer does not have his block locally
            if peer.online and peer.current_download is None and not peer.local_blocks[block_id]: # +TODO
                sim.schedule_transfer(self, peer, block_id, restore=True)                       # +TODO
                return  # it means that that peer lost the block and we are uploading it to him

        # 2. if other nodes do not require repair, 
        # we look for our own blocks which are not backed up yet
        block_id = self.find_block_to_back_up()
        if block_id is None:
            return
        
        # 3. collecting the nodes that have at least one our block backed up
        remote_owners = set(node for node in self.backed_up_blocks if node is not None)  # nodes having one block
        for peer in sim.nodes:
        # 4. we look for a node:
        # if the peer is not self, is online, is not among the remote owners, has enough space and is not
        # downloading anything currently, schedule the backup of block_id from self to peer
            if (peer is not self and peer.online and peer not in remote_owners and peer.current_download is None # +TODO
                    and peer.free_space >= self.block_size):                                                    # +TODO
                sim.schedule_transfer(self, peer, block_id, restore=False)                                    # +TODO
                return

    def schedule_next_download(self, sim: Backup):
        """Schedule the next download, if any."""

        assert self.online  # we can only download if we're online

        # we don't want to download if we're already downloading something
        if self.current_download is not None:
            return

        # 1. find if we have a block which is on a remote node and not on our node
        for block_id, (held_locally, peer) in enumerate(zip(self.local_blocks, self.backed_up_blocks)):
            if not held_locally and peer is not None and peer.online and peer.current_upload is None:  # +TODO
                sim.schedule_transfer(peer, self, block_id, restore=True)                           # +TODO
                return  # it means that we are downloading the block from the remote peer

        # 2. we look for a node
        for peer in sim.nodes:
            # if the peer is not self, is online, has no current upload, is not among the remote owners,
            # has enough space and is not downloading anything currently, schedule the backup of block_id from self to peer
            if (peer is not self and peer.online and peer.current_upload is None and peer not in self.remote_blocks_held # +TODO
                    and self.free_space >= peer.block_size):                                                # +TODO                                     
                block_id = peer.find_block_to_back_up()
                if block_id is not None:
                    sim.schedule_transfer(peer, self, block_id, restore=False)                               # +TODO
                    return

    def __hash__(self):
        """Function that allows us to have `Node`s as dictionary keys or set items.

        With this implementation, each node is only equal to itself.
        """
        return id(self)

    def __str__(self):
        """Function that will be called when converting this to a string (e.g., when logging or printing)."""

        return self.name


@dataclass
class NodeEvent(Event):
    """An event regarding a node. Carries the identifier, i.e., the node's index in `Backup.nodes_config`"""

    node: Node

    def process(self, sim: Simulation):
        """Must be implemented by subclasses."""
        raise NotImplementedError


class Online(NodeEvent):
    """A node goes online."""

    def process(self, sim: Backup):
        node = self.node
        # we do nothing if the node is already online or failed
        if node.online or node.failed:
            return
        # otherwise set the node as online
        node.online = True
        # the node looks for which blocks are possible to back up
        # and which blocks are possible to download
        node.schedule_next_upload(sim) # class Node # +TODO
        node.schedule_next_download(sim) # class Node   # +TODO

        # schedule the next offline event
        sim.schedule(exp_rv(node.average_uptime), Offline(node))   # +TODO


class Recover(Online):
    """A node goes online after recovering from a failure."""

    def process(self, sim: Backup):
        node = self.node
        sim.log_info(f"{node} recovers")
        node.failed = False
        super().process(sim)
        sim.schedule(exp_rv(node.average_lifetime), Fail(node))


class Disconnection(NodeEvent):
    """Base class for both Offline and Fail, events that make a node disconnect."""

    def process(self, sim: Simulation):
        """Must be implemented by subclasses"""
        raise NotImplementedError

    def disconnect(self):
        node = self.node
        # 1. set the node as offline
        node.online = False
        # cancel current upload and download
        # retrieve the nodes we're uploading and downloading to 
        # and set their current downloads and uploads to None
        current_upload, current_download = node.current_upload, node.current_download
        # 2. if the node was uploading something:
        # set uploading as False, remove link on the remote downloader,
        # remove link on the uploader (the node)
        if current_upload is not None:
            current_upload.canceled = True
            current_upload.downloader.current_download = None
            node.current_upload = None
        # 3. if the node was downloading something:
        # set downloading as False, remove link on the remote uploader,
        # remove link on the downloader (the node)
        if current_download is not None:
            current_download.canceled = True
            current_download.uploader.current_upload = None
            node.current_download = None


class Offline(Disconnection):
    """A node goes offline."""

    def process(self, sim: Backup):
        node = self.node
        # do nothing if the node is already offline or failed
        if node.failed or not node.online:
            return
        assert node.online
        # if the node is online, set it as offline
        self.disconnect() # from the parent class Disconnection
        # schedule when the node will be back online
        sim.schedule(exp_rv(self.node.average_downtime), Online(node))


class Fail(Disconnection):
    """A node fails and loses all local data."""

    def process(self, sim: Backup):
        sim.log_info(f"{self.node} fails")
        self.disconnect()
        node = self.node
        node.failed = True

        # log data loss
        node.total_data_loss_events += 1


        # all the local data is lost
        node.local_blocks = [False] * node.n  # lose all local data
        # all the remote blocks which were held on this node are lost
        for owner, block_id in node.remote_blocks_held.items():
            owner.backed_up_blocks[block_id] = None
            # if the owner is online and has no current upload, schedule the next upload to other nodes
            if owner.online and owner.current_upload is None:
                owner.schedule_next_upload(sim)  # this node may want to back up the missing block
        # clean info about the remote blocks held on this node
        node.remote_blocks_held.clear()
        # set the free space to the storage size of this node
        node.free_space = node.storage_size - node.block_size * node.n
        # schedule the next online and recover events
        recover_time = exp_rv(node.average_recover_time)
        # в Fail.handle()
        #self.node.local_blocks = [False] * self.node.n
        #self.node.total_data_loss_events += 1
        node.local_blocks = [False] * self.node.n
        node.total_data_loss_events += 1

        # schedule the recovery event of this node in a random exponentioal time
        sim.schedule(recover_time, Recover(node))


@dataclass
class TransferComplete(Event):
    """An upload is completed."""

    uploader: Node
    downloader: Node
    block_id: int
    # was the transfer canceled
    canceled: bool = False

    def __post_init__(self):
        assert self.uploader is not self.downloader

    def process(self, sim: Backup):
        sim.log_info(f"{self.__class__.__name__} from {self.uploader} to {self.downloader}")
        if self.canceled:
            return  # this transfer was canceled, so ignore this event
        uploader, downloader = self.uploader, self.downloader
        assert uploader.online and downloader.online
        # it updates the state of the uploader and downloader
        # it's implemented in the BlockBackupComplete and BlockRestoreComplete classes
        self.update_block_state()
        downloader.current_download = None
        uploader.current_upload = None
        # plan the next upload and download of the uploader and downloader nodes
        uploader.schedule_next_upload(sim)
        downloader.schedule_next_download(sim)
        # log the state of the nodes
        for node in [uploader, downloader]:
            sim.log_info(f"{node}: {sum(node.local_blocks)} local blocks, "
                         f"{sum(peer is not None for peer in node.backed_up_blocks)} backed up blocks, "
                         f"{len(node.remote_blocks_held)} remote blocks held")

    def update_block_state(self):
        """Needs to be specified by the subclasses, `BackupComplete` and `DownloadComplete`."""
        raise NotImplementedError


class BlockBackupComplete(TransferComplete):

    def update_block_state(self):
        owner, peer = self.uploader, self.downloader
        peer.free_space = peer.free_space - owner.block_size
        assert peer.free_space >= 0
        # now the owner knows that his block is backed up on the peer
        owner.backed_up_blocks[self.block_id] = peer
        # now the peer knows that he has a block from the owner
        peer.remote_blocks_held[owner] = self.block_id

        owner.backed_up_blocks[self.block_id] = self.downloader
        owner.total_backups_made += 1 # ???



class BlockRestoreComplete(TransferComplete):
    def update_block_state(self):
        owner = self.downloader
        # now the owner knows that his block is held locally 
        owner.local_blocks[self.block_id] = True
        owner.total_restores_made += 1
        if sum(owner.local_blocks) == owner.k:  # we have exactly k local blocks, we have all of them then
            self.downloader.total_data_recovered += 1        # +TODO


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config", help="configuration file")
    parser.add_argument("--max-t", default="100 years")
    parser.add_argument("--seed", help="random seed")
    parser.add_argument("--verbose", action='store_true')
    parser.add_argument("--summary", action="store_true", help="print simulation summary")

    args = parser.parse_args()

    if args.seed:
        random.seed(args.seed)  # set a seed to make experiments repeatable
    if args.verbose:
        logging.basicConfig(format='{levelname}:{message}', level=logging.INFO, style='{')  # output info on stdout

    # functions to parse every parameter of peer configuration
    parsing_functions = [
        ('n', int), ('k', int),
        ('data_size', parse_size), ('storage_size', parse_size),
        ('upload_speed', parse_size), ('download_speed', parse_size),
        ('average_uptime', parse_timespan), ('average_downtime', parse_timespan),
        ('average_lifetime', parse_timespan), ('average_recover_time', parse_timespan),
        ('arrival_time', parse_timespan)
    ]

    config = configparser.ConfigParser()
    config.read(args.config)
    nodes = []  # we build the list of nodes to pass to the Backup class
    for node_class in config.sections():
        class_config = config[node_class]
        # list comprehension: https://docs.python.org/3/tutorial/datastructures.html#list-comprehensions
        cfg = [parse(class_config[name]) for name, parse in parsing_functions]
        # the `callable(p1, p2, *args)` idiom is equivalent to `callable(p1, p2, args[0], args[1], ...)
        nodes.extend(Node(f"{node_class}-{i}", *cfg) for i in range(class_config.getint('number')))
    sim = Backup(nodes)
    sim.run(parse_timespan(args.max_t))
    sim.log_info(f"Simulation over")
    if args.summary:
        sim.summary()



if __name__ == '__main__':
    main()
