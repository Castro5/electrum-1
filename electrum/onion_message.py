# Electrum - Lightweight Bitcoin Client
# Copyright (c) 2023-2024 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
import asyncio
import copy
import io
import os
import queue
import threading
from random import random

from typing import TYPE_CHECKING, Optional, List, Sequence

import electrum_ecc as ecc

from electrum.lnrouter import PathEdge
from electrum.logging import get_logger, Logger
from electrum.crypto import sha256, get_ecdh
from electrum.lnmsg import OnionWireSerializer
from electrum.lnonion import (get_bolt04_onion_key, OnionPacket, process_onion_packet,
                              OnionHopsDataSingle, decrypt_encrypted_data_tlv, encrypt_encrypted_data_tlv,
                              get_shared_secrets_along_route, new_onion_packet)
from electrum.lnutil import LnFeatures
from electrum.util import OldTaskGroup, now

if TYPE_CHECKING:
    from electrum.lnworker import LNWallet
    from electrum.network import Network
    from electrum.lnrouter import NodeInfo
    from asyncio import Task

logger = get_logger(__name__)


REQUEST_REPLY_TIMEOUT = 30
REQUEST_REPLY_RETRY_DELAY = 5
REQUEST_REPLY_PATHS_MAX = 3
FORWARD_RETRY_TIMEOUT = 4
FORWARD_RETRY_DELAY = 2
FORWARD_MAX_QUEUE = 3

WELLKNOWN_ONIONMSG_NODES = [
    '03864ef025fde8fb587d989186ce6a4a186895ee44a926bfc370e2c366597a3f8f@3.33.236.230:9735',  # ACINQ/Phoenix
    # others?
]


def create_blinded_path(session_key: bytes, path: List[bytes], final_recipient_data: dict, *,
                        hop_extras: Optional[Sequence[dict]] = None,
                        dummy_hops: Optional[int] = 0) -> dict:
    # dummy hops could be inserted anywhere in the path, but for compatibility just add them at the end
    # because blinded paths are usually constructed towards ourselves, and we know we can handle dummy hops.
    if dummy_hops:
        logger.debug(f'adding {dummy_hops} dummy hops at the end')
        path += [path[-1]] * dummy_hops

    introduction_point = path[0]

    blinding = ecc.ECPrivkey(session_key).get_public_key_bytes()

    onionmsg_hops = []
    shared_secrets, blinded_node_ids = get_shared_secrets_along_route(path, session_key, with_blinded_node_ids=True)
    for i, node_id in enumerate(path):
        is_non_final_node = i < len(path) - 1

        if is_non_final_node:
            recipient_data = {
                # TODO: SHOULD add padding data to ensure all encrypted_data_tlv(i) have the same length
                'next_node_id': {'node_id': path[i+1]}
            }
            if hop_extras and i < len(hop_extras):  # extra hop data for debugging for now
                recipient_data.update(hop_extras[i])
        else:
            # TODO: SHOULD add padding data to ensure all encrypted_data_tlv(i) have the same length
            recipient_data = final_recipient_data

        encrypted_recipient_data = encrypt_encrypted_data_tlv(shared_secret=shared_secrets[i], **recipient_data)

        hopdata = {
            'blinded_node_id': blinded_node_ids[i],
            'enclen': len(encrypted_recipient_data),
            'encrypted_recipient_data': encrypted_recipient_data
        }
        onionmsg_hops.append(hopdata)

    blinded_path = {
        'first_node_id': introduction_point,
        'blinding': blinding,
        'num_hops': len(onionmsg_hops),
        'path': onionmsg_hops
    }

    return blinded_path


def blinding_privkey(privkey, blinding):
    shared_secret = get_ecdh(privkey, blinding)
    b_hmac = get_bolt04_onion_key(b'blinded_node_id', shared_secret)
    b_hmac_int = int.from_bytes(b_hmac, byteorder="big")

    our_privkey_int = int.from_bytes(privkey, byteorder="big")
    our_privkey_int = our_privkey_int * b_hmac_int % ecc.CURVE_ORDER
    our_privkey = our_privkey_int.to_bytes(32, byteorder="big")

    return our_privkey


def is_onion_message_node(node_id: bytes, node_info: Optional['NodeInfo']):
    if not node_info:
        return False
    return LnFeatures(node_info.features).supports(LnFeatures.OPTION_ONION_MESSAGE_OPT)


def encrypt_onionmsg_tlv_hops_data(hops_data, hop_shared_secrets):
    """encrypt unencrypted onionmsg_tlv.encrypted_recipient_data for hops with blind_fields"""
    num_hops = len(hops_data)
    for i in range(num_hops):
        if hops_data[i].tlv_stream_name == 'onionmsg_tlv' and 'encrypted_recipient_data' not in hops_data[i].payload:
            # construct encrypted_recipient_data from blind_fields
            encrypted_recipient_data = encrypt_encrypted_data_tlv(shared_secret=hop_shared_secrets[i], **hops_data[i].blind_fields)
            hops_data[i].payload['encrypted_recipient_data'] = {'encrypted_recipient_data': encrypted_recipient_data}


async def create_onion_message_route_to(lnwallet: 'LNWallet', node_id: bytes) -> List[PathEdge]:
    """Constructs a route to the destination node_id, first by starting with peers with existing channels,
       and if no route found, opening a direct peer connection if node_id is found with an address in
       channel_db."""
    # TODO: is this the proper way to set up my_sending_channels?
    my_active_channels = [
        chan for chan in lnwallet.channels.values() if
        chan.is_active() and not chan.is_frozen_for_sending()]
    my_sending_channels = {chan.short_channel_id: chan for chan in my_active_channels
                           if chan.short_channel_id is not None}
    # strat1: find route to introduction point over existing channel mesh
    # NOTE: nodes that are in channel_db but are offline are not removed from the set
    if lnwallet.network.path_finder:
        if path := lnwallet.network.path_finder.find_path_for_payment(
            nodeA=lnwallet.node_keypair.pubkey,
            nodeB=node_id,
            invoice_amount_msat=10000,  # TODO: do this without amount constraints
            node_filter=lambda x, y: True if x == lnwallet.node_keypair.pubkey else is_onion_message_node(x, y),
            my_sending_channels=my_sending_channels
        ): return path

    # strat2: dest node has host:port in channel_db? then open direct peer connection
    if lnwallet.channel_db:
        if peer_addr := lnwallet.channel_db.get_last_good_address(node_id):
            peer = await lnwallet.add_peer(str(peer_addr))
            await peer.initialized
            return [PathEdge(short_channel_id=None, start_node=None, end_node=node_id)]

    raise Exception('no path found')


async def send_onion_message_to(lnwallet: 'LNWallet', node_id_or_blinded_path: bytes, destination_payload: dict, session_key: bytes = None):
    if session_key is None:
        session_key = os.urandom(32)

    if len(node_id_or_blinded_path) > 33:  # assume blinded path
        with io.BytesIO(node_id_or_blinded_path) as blinded_path_fd:
            try:
                blinded_path = OnionWireSerializer._read_complex_field(fd=blinded_path_fd,
                                                                       field_type='blinded_path',
                                                                       count=1)
                logger.debug(f'blinded path: {blinded_path!r}')
            except Exception as e:
                logger.error(f'e!r')
                raise

            introduction_point = blinded_path['first_node_id']

            hops_data = []
            blinded_node_ids = []

            if lnwallet.node_keypair.pubkey == introduction_point:
                # blinded path introduction point is me
                our_blinding = blinded_path['blinding']
                our_payload = blinded_path['path'][0]
                remaining_blinded_path = blinded_path['path'][1:]
                assert len(remaining_blinded_path) > 0, 'sending to myself?'

                # decrypt
                shared_secret = get_ecdh(lnwallet.node_keypair.privkey, our_blinding)
                recipient_data = decrypt_encrypted_data_tlv(
                    shared_secret=shared_secret,
                    encrypted_recipient_data=our_payload['encrypted_recipient_data']
                )

                peer = lnwallet.peers.get(recipient_data['next_node_id']['node_id'])
                assert peer, 'next_node_id not a peer'

                # blinding override?
                next_blinding_override = recipient_data.get('next_blinding_override')
                if next_blinding_override:
                    next_blinding = next_blinding_override.get('blinding')
                else:
                    # E_i+1=SHA256(E_i||ss_i) * E_i
                    blinding_factor = sha256(our_blinding + shared_secret)
                    blinding_factor_int = int.from_bytes(blinding_factor, byteorder="big")
                    next_public_key_int = ecc.ECPubkey(our_blinding) * blinding_factor_int
                    next_blinding = next_public_key_int.get_public_key_bytes()

                blinding = next_blinding

            else:
                # we need a route to introduction point
                remaining_blinded_path = blinded_path['path']
                peer = lnwallet.peers.get(introduction_point)
                # if blinded path introduction point is our direct peer, no need to route-find
                if peer:
                    # start of blinded path is our peer
                    blinding = blinded_path['blinding']
                else:
                    path = await create_onion_message_route_to(lnwallet, introduction_point)

                    # first edge must be to our peer
                    peer = lnwallet.peers.get(path[0].end_node)
                    assert peer, 'first hop not a peer'

                    # last edge is to introduction point and start of blinded path. remove from route
                    assert path[-1].end_node == introduction_point, 'last hop in route must be introduction point'

                    path = path[:-1]

                    if len(path) == 0:
                        blinding = blinded_path['blinding']
                    else:
                        payment_path_pubkeys = [edge.end_node for edge in path]
                        hop_shared_secrets, blinded_node_ids = get_shared_secrets_along_route(payment_path_pubkeys,
                                                                                              session_key,
                                                                                              with_blinded_node_ids=True)

                        hops_data = [
                            OnionHopsDataSingle(
                                tlv_stream_name='onionmsg_tlv',
                                blind_fields={'next_node_id': {'node_id': x.end_node}}
                            ) for x in path[:-1]
                        ]

                        # final hop pre-ip, add next_blinding_override
                        final_hop_pre_ip = OnionHopsDataSingle(
                            tlv_stream_name='onionmsg_tlv',
                            blind_fields={'next_node_id': {'node_id': introduction_point},
                                          'next_blinding_override': {'blinding': blinded_path['blinding']},
                                          }
                        )

                        hops_data.append(final_hop_pre_ip)

                        # encrypt encrypted_data_tlv here
                        for i in range(len(hops_data)):
                            encrypted_recipient_data = encrypt_encrypted_data_tlv(shared_secret=hop_shared_secrets[i],
                                                                                  **hops_data[i].blind_fields)
                            hops_data[i].payload['encrypted_recipient_data'] = {
                                'encrypted_recipient_data': encrypted_recipient_data
                            }

                        blinding = ecc.ECPrivkey(session_key).get_public_key_bytes()

            # append (remaining) blinded path and payload
            blinded_path_blinded_ids = []
            for i, onionmsg_hop in enumerate(remaining_blinded_path):
                blinded_path_blinded_ids.append(onionmsg_hop.get('blinded_node_id'))
                payload = {
                    'encrypted_recipient_data': {'encrypted_recipient_data': onionmsg_hop['encrypted_recipient_data']}
                }
                if i == len(remaining_blinded_path) - 1:  # final hop
                    payload.update(destination_payload)
                hop = OnionHopsDataSingle(tlv_stream_name='onionmsg_tlv', payload=payload)
                hops_data.append(hop)

            payment_path_pubkeys = blinded_node_ids + blinded_path_blinded_ids
            hop_shared_secrets = get_shared_secrets_along_route(payment_path_pubkeys, session_key)
            encrypt_onionmsg_tlv_hops_data(hops_data, hop_shared_secrets)
            packet = new_onion_packet(payment_path_pubkeys, session_key, hops_data)
            packet_b = packet.to_bytes()

    else:  # node pubkey
        pubkey = node_id_or_blinded_path

        if lnwallet.node_keypair.pubkey == pubkey:
            raise Exception('cannot send to myself')

        hops_data = []
        peer = lnwallet.peers.get(pubkey)

        if peer:
            # destination is our direct peer, no need to route-find
            path = [PathEdge(short_channel_id=None, start_node=None, end_node=pubkey)]
        else:
            path = await create_onion_message_route_to(lnwallet, pubkey)

            # first edge must be to our peer
            peer = lnwallet.peers.get(path[0].end_node)
            assert peer, 'first hop not a peer'

            hops_data = [
                OnionHopsDataSingle(
                    tlv_stream_name='onionmsg_tlv',
                    blind_fields={'next_node_id': {'node_id': x.end_node}}
                ) for x in path[1:]
            ]

        final_hop = OnionHopsDataSingle(
            tlv_stream_name='onionmsg_tlv',
            payload=destination_payload
        )

        hops_data.append(final_hop)

        payment_path_pubkeys = [edge.end_node for edge in path]

        hop_shared_secrets, blinded_node_ids = get_shared_secrets_along_route(payment_path_pubkeys, session_key,
                                                                              with_blinded_node_ids=True)
        encrypt_onionmsg_tlv_hops_data(hops_data, hop_shared_secrets)
        packet = new_onion_packet(blinded_node_ids, session_key, hops_data)
        packet_b = packet.to_bytes()

        blinding = ecc.ECPrivkey(session_key).get_public_key_bytes()

    peer.send_message(
        "onion_message",
        blinding=blinding,
        len=len(packet_b),
        onion_message_packet=packet_b
    )


async def get_blinded_reply_paths(lnwallet: 'LNWallet', path_id: bytes, *,
                                  max_paths: int = REQUEST_REPLY_PATHS_MAX,
                                  preferred_node_id: bytes = None) -> List[dict]:
    # TODO: build longer paths and/or add dummy hops to increase privacy
    my_active_channels = [chan for chan in lnwallet.channels.values() if chan.is_active()]
    my_onionmsg_channels = [chan for chan in my_active_channels if lnwallet.peers.get(chan.node_id) and
                            lnwallet.peers.get(chan.node_id).their_features.supports(LnFeatures.OPTION_ONION_MESSAGE_OPT)]
    my_onionmsg_peers = [peer for peer in lnwallet.peers.values() if peer.their_features.supports(LnFeatures.OPTION_ONION_MESSAGE_OPT)]

    result = []
    mynodeid = lnwallet.node_keypair.pubkey
    mydata = {'path_id': {'data': path_id}}  # same used in every path
    if len(my_onionmsg_channels):
        # randomize list, but prefer preferred_node_id
        rchans = sorted(my_onionmsg_channels, key=lambda x: random() if x.node_id != preferred_node_id else 0)
        for chan in rchans[:max_paths]:
            blinded_path = create_blinded_path(os.urandom(32), [chan.node_id, mynodeid], mydata)
            result.append(blinded_path)
    elif len(my_onionmsg_peers):
        # randomize list, but prefer preferred_node_id
        rpeers = sorted(my_onionmsg_peers, key=lambda x: random() if x.pubkey != preferred_node_id else 0)
        for peer in rpeers[:max_paths]:
            blinded_path = create_blinded_path(os.urandom(32), [peer.pubkey, mynodeid], mydata)
            result.append(blinded_path)
    else:
        # connect to a well-known peer if no route can be
        # TODO: prefer node from config?
        wellknown = sorted(WELLKNOWN_ONIONMSG_NODES, key=lambda x: random())[0]
        logger.debug(f'no path for existing peers, connecting {wellknown=}')
        peer = await lnwallet.add_peer(str(wellknown))
        await peer.initialized
        blinded_path = create_blinded_path(os.urandom(32), [peer.pubkey, mynodeid], mydata)
        result.append(blinded_path)

    return result


class Timeout(Exception): pass


class OnionMessageManager(Logger):
    """handle state around onion message sends and receives
    - association between onion message and their replies
    - manage re-send attempts, TODO: iterate through routes (both directions)"""

    def __init__(self, lnwallet: 'LNWallet'):
        Logger.__init__(self)
        self.network = None  # type: Optional['Network']
        self.taskgroup = None  # type: OldTaskGroup
        self.lnwallet = lnwallet

        self.pending = {}
        self.pending_lock = threading.Lock()
        self.reqrpyqueue = queue.PriorityQueue()
        self.reqrpyqueue_notempty = asyncio.Event()
        self.forwardqueue = queue.PriorityQueue()
        self.forwardqueue_notempty = asyncio.Event()

    def start_network(self, *, network: 'Network'):
        assert network
        assert self.network is None, "already started"
        self.network = network
        self.taskgroup = OldTaskGroup()
        asyncio.run_coroutine_threadsafe(self.main_loop(), self.network.asyncio_loop)

    async def main_loop(self):
        self.logger.info("starting taskgroup.")
        try:
            async with self.taskgroup as group:
                await group.spawn(self.process_request_reply_queue())
                await group.spawn(self.process_forward_queue())
        except Exception as e:
            self.logger.exception("taskgroup died.")
        else:
            self.logger.info("taskgroup stopped.")

    async def stop(self):
        await self.taskgroup.cancel_remaining()

    async def process_forward_queue(self):
        while True:
            try:
                scheduled, expires, onion_packet, blinding, node_id = self.forwardqueue.get_nowait()
            except queue.Empty:
                self.logger.debug(f'fwd queue empty')
                self.forwardqueue_notempty.clear()
                await self.forwardqueue_notempty.wait()
                continue

            if expires <= now():
                self.logger.debug(f'fwd expired {node_id=}')
                continue
            if scheduled > now():
                # return to queue
                self.forwardqueue.put_nowait((scheduled, expires, onion_packet, blinding, node_id))
                await asyncio.sleep(1)  # sleep here, as the first queue item wasn't due yet
                continue

            try:
                onion_packet_b = onion_packet.to_bytes()
                next_peer = self.lnwallet.peers.get(node_id)

                next_peer.send_message(
                    "onion_message",
                    blinding=blinding,
                    len=len(onion_packet_b),
                    onion_message_packet=onion_packet_b
                )
            except BaseException as e:
                self.logger.debug(f'error while sending {node_id=} e={e!r}')
                self.forwardqueue.put_nowait((now() + FORWARD_RETRY_DELAY, expires, onion_packet, blinding, node_id))

    def submit_forward(self, *,
                       onion_packet: OnionPacket,
                       blinding: bytes,
                       node_id: bytes):
        if self.forwardqueue.qsize() >= FORWARD_MAX_QUEUE:
            self.logger.debug('fwd queue full, dropping packet')
            return
        expires = now() + FORWARD_RETRY_TIMEOUT
        queueitem = (now(), expires, onion_packet, blinding, node_id)
        self.forwardqueue.put_nowait(queueitem)
        self.forwardqueue_notempty.set()

    async def process_request_reply_queue(self):
        while True:
            try:
                scheduled, expires, key = self.reqrpyqueue.get_nowait()
            except queue.Empty:
                self.logger.debug(f'reqrpy queue empty')
                self.reqrpyqueue_notempty.clear()
                await self.reqrpyqueue_notempty.wait()
                continue

            reqrpy = self.get_reqrpy(key)
            if reqrpy is None:
                self.logger.debug(f'no data for key {key=}')
                continue
            if reqrpy.get('result') is not None:
                self.logger.debug(f'has result! {key=}')
                continue
            if expires <= now():
                self.logger.debug(f'expired {key=}')
                self._set_reqrpy_result(key, Timeout())
                continue
            if scheduled > now():
                # return to queue
                self.reqrpyqueue.put_nowait((scheduled, expires, key))
                await asyncio.sleep(1)  # sleep here, as the first queue item wasn't due yet
                continue

            try:
                await self._send_pending_reqrpy(key)
            except BaseException as e:
                self.logger.debug(f'error while sending {key=}')
                self._set_reqrpy_result(key, e)
            else:
                self.reqrpyqueue.put_nowait((now() + REQUEST_REPLY_RETRY_DELAY, expires, key))

    def get_reqrpy(self, key):
        with self.pending_lock:
            return self.pending.get(key)

    def _set_reqrpy_result(self, key, result):
        with self.pending_lock:
            reqrpy = self.pending.get(key)
            if reqrpy is None:
                return
            self.pending[key]['result'] = result
        reqrpy['ev'].set()

    def _remove_reqrpy(self, key):
        with self.pending_lock:
            reqrpy = self.pending.get(key)
            if reqrpy is None:
                return
            reqrpy['ev'].set()
            del self.pending[key]

    def submit_reqrpy(self, *,
                      payload: dict,
                      node_id_or_blinded_path: bytes,
                      key: bytes = None) -> 'Task':
        """Add onion message to queue for sending. Queued onion message payloads
           are supplied with a path_id and a reply_path to determine which request
           corresponds with arriving replies.

           If caller has provided 'reply_path' in payload, caller should also provide associating key.

           :return: returns awaitable task"""
        if not key:
            key = os.urandom(8)
        assert type(key) is bytes and len(key) >= 8

        self.logger.debug(f'submit_reqrpy {key=} {payload=} {node_id_or_blinded_path=}')

        with self.pending_lock:
            if key in self.pending:
                raise Exception(f'{key=} already exists!')
            self.pending[key] = {
                'ev': asyncio.Event(),
                'payload': payload,
                'node_id_or_blinded_path': node_id_or_blinded_path
            }

        # tuple = (when to process, when it expires, key)
        expires = now() + REQUEST_REPLY_TIMEOUT
        queueitem = (now(), expires, key)
        self.reqrpyqueue.put_nowait(queueitem)
        task = asyncio.create_task(self._reqrpy_task(key))
        self.reqrpyqueue_notempty.set()
        return task

    async def _reqrpy_task(self, key):
        reqrpy = self.get_reqrpy(key)
        assert reqrpy
        if reqrpy is None:
            return
        try:
            self.logger.debug(f'wait task start {key}')
            await reqrpy['ev'].wait()
        finally:
            self.logger.debug(f'wait task end {key}')

        try:
            reqrpy = self.get_reqrpy(key)
            assert reqrpy
            result = reqrpy.get('result')
            if isinstance(result, Exception):
                raise result
            return result
        finally:
            self._remove_reqrpy(key)

    async def _send_pending_reqrpy(self, key):
        """adds reply_path to payload"""
        data = self.get_reqrpy(key)
        payload = data.get('payload')
        node_id_or_blinded_path = data.get('node_id_or_blinded_path')
        self.logger.debug(f'send_reqrpy {key=} {payload=} {node_id_or_blinded_path=}')

        final_payload = copy.deepcopy(payload)

        if 'reply_path' not in final_payload:
            # unless explicitly set in payload, generate reply_path here
            path_id = self._path_id_from_payload_and_key(payload, key)
            reply_paths = await get_blinded_reply_paths(self.lnwallet, path_id, max_paths=1)
            if not reply_paths:
                raise Exception(f'Could not create a reply_path for {key=}')

            final_payload['reply_path'] = {'path': reply_paths}

        # TODO: we should try alternate paths when retrying, this is currently not done.
        # (send_onion_message_to decides path, without knowledge of prev attempts)
        await send_onion_message_to(self.lnwallet, node_id_or_blinded_path, final_payload)

    def _path_id_from_payload_and_key(self, payload: dict, key: bytes) -> bytes:
        # TODO: construct path_id in such a way that we can determine the request originated from us and is not spoofed
        # TODO: use payload to determine prefix?
        return b'electrum' + key

    def on_onion_message_received(self, recipient_data, payload):
        # we are destination, sanity checks
        # - if `encrypted_data_tlv` contains `allowed_features`:
        #   - MUST ignore the message if:
        #     - `encrypted_data_tlv.allowed_features.features` contains an unknown feature bit (even if it is odd).
        #     - the message uses a feature not included in `encrypted_data_tlv.allowed_features.features`.
        if 'allowed_features' in recipient_data:
            pass  # TODO

        # - if `path_id` is set and corresponds to a path the reader has previously published in a `reply_path`:
        #   - if the onion message is not a reply to that previous onion:
        #     - MUST ignore the onion message
        # TODO: store path_id and lookup here
        if 'path_id' not in recipient_data:
            # unsolicited onion_message
            self.on_onion_message_received_unsolicited(recipient_data, payload)
        else:
            self.on_onion_message_received_reply(recipient_data, payload)

    def on_onion_message_received_reply(self, recipient_data, payload):
        # check if this reply is associated with a known request
        correl_data = recipient_data['path_id'].get('data')
        if not correl_data[:8] == b'electrum':
            self.logger.warning('not a reply to our request (unknown path_id prefix)')
            return
        key = correl_data[8:]
        reqrpy = self.get_reqrpy(key)
        if reqrpy is None:
            self.logger.warning('not a reply to our request (unknown request)')
            return

        self._set_reqrpy_result(key, (recipient_data, payload))

    def on_onion_message_received_unsolicited(self, recipient_data, payload):
        self.logger.debug('unsolicited onion_message received')
        self.logger.debug(f'payload: {payload!r}')

        # TODO: currently only accepts simple text 'message' payload.

        if 'message' not in payload:
            self.logger.error('Unsupported onion message payload')
            return

        if 'text' not in payload['message'] or not isinstance(payload['message']['text'], bytes):
            self.logger.error('Malformed \'message\' payload')
            return

        try:
            text = payload['message']['text'].decode('utf-8')
        except Exception as e:
            self.logger.error(f'Malformed \'message\' payload: {e!r}')
            return

        self.logger.info(f'onion message with text received: {text}')

    def on_onion_message_forward(self, recipient_data, onion_packet, blinding, shared_secret):
        if recipient_data.get('path_id'):
            self.logger.error('cannot forward onion_message, path_id in encrypted_data_tlv')
            return

        next_node_id = recipient_data.get('next_node_id')
        if not next_node_id:
            self.logger.error('cannot forward onion_message, next_node_id missing in encrypted_data_tlv')
            return
        next_node_id = next_node_id['node_id']

        is_dummy_hop = False
        if next_node_id == self.lnwallet.node_keypair.pubkey:
            self.logger.debug('dummy hop')
            is_dummy_hop = True
        else:
            # is next_node one of our peers?
            next_peer = self.lnwallet.peers.get(next_node_id)
            if not next_peer:
                self.logger.info(f'next node {next_node_id.hex()} not a peer, dropping message')
                return

        # blinding override?
        next_blinding_override = recipient_data.get('next_blinding_override')
        if next_blinding_override:
            next_blinding = next_blinding_override.get('blinding')
        else:
            # E_i+1=SHA256(E_i||ss_i) * E_i
            blinding_factor = sha256(blinding + shared_secret)
            blinding_factor_int = int.from_bytes(blinding_factor, byteorder="big")
            next_public_key_int = ecc.ECPubkey(blinding) * blinding_factor_int
            next_blinding = next_public_key_int.get_public_key_bytes()

        if is_dummy_hop:
            self.process_onion_message_packet(next_blinding, onion_packet)
            return

        self.submit_forward(onion_packet=onion_packet, blinding=next_blinding, node_id=next_node_id)

    def on_onion_message(self, payload):
        blinding = payload.get('blinding')
        if not blinding:
            self.logger.error('missing blinding')
            return
        packet = payload.get('onion_message_packet')
        if payload.get('len', 0) != len(packet):
            self.logger.error('invalid/missing length')
            return

        self.logger.debug('handling onion message')

        onion_packet = OnionPacket.from_bytes(packet)
        self.process_onion_message_packet(blinding, onion_packet)

    def process_onion_message_packet(self, blinding: bytes, onion_packet: OnionPacket):
        our_privkey = blinding_privkey(self.lnwallet.node_keypair.privkey, blinding)
        processed_onion_packet = process_onion_packet(onion_packet, our_privkey, tlv_stream_name='onionmsg_tlv')
        payload = processed_onion_packet.hop_data.payload

        self.logger.debug(f'onion peeled: {processed_onion_packet!r}')

        if not processed_onion_packet.are_we_final:
            if any([x not in ['encrypted_recipient_data'] for x in payload.keys()]):
                self.logger.error('unexpected data in payload')  # non-final nodes only encrypted_recipient_data
                return

        # decrypt
        shared_secret = get_ecdh(self.lnwallet.node_keypair.privkey, blinding)
        recipient_data = decrypt_encrypted_data_tlv(
            shared_secret=shared_secret,
            encrypted_recipient_data=payload['encrypted_recipient_data']['encrypted_recipient_data']
        )

        self.logger.debug(f'parsed recipient_data: {recipient_data!r}')

        if processed_onion_packet.are_we_final:
            self.on_onion_message_received(recipient_data, payload)
        else:
            self.on_onion_message_forward(recipient_data, processed_onion_packet.next_packet, blinding, shared_secret)
