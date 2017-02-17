# -*- coding: utf-8 -*-
from raiden.transfer.architecture import Iteration
from raiden.transfer.mediated_transfer.transition import update_route
from raiden.transfer.mediated_transfer.state import (
    MediatorState,
    MediationPairState,
    LockedTransferState,
)
from raiden.transfer.mediated_transfer.state_change import (
    ReceiveBalanceProof,
    ActionInitMediator,
    ContractReceiveWithdraw,
    ReceiveSecretReveal,
    ReceiveTransferRefund,
)
from raiden.transfer.state_change import (
    Block,
    ActionRouteChange,
)
from raiden.transfer.mediated_transfer.events import (
    mediatedtransfer,

    ContractSendWithdraw,
    SendBalanceProof,
    SendRefundTransfer,
    SendRevealSecret,
)
from raiden.utils import sha3

# Reduce the lock expiration by some additional blocks to prevent this exploit:
# The payee could reveal the secret on it's lock expiration block, the lock
# would be valid and the previous lock can be safely unlocked so the mediator
# would follow the secret reveal with a balance-proof, at this point the secret
# is know, the payee transfer is payed, and if the payer expiration is exactly
# reveal_timeout blocks away the mediator will be forced to close the channel
# to be safe.
TRANSIT_BLOCKS = 2  # TODO: make this a configuration variable


STATE_SECRET_KNOWN = (
    'payee_secret_revealed',
    'payee_contract_withdraw',
    'payee_balance_proof',

    'payer_secret_revealed',
    'payer_contract_withdraw',
    'payer_balance_proof',
)
STATE_TRANSFER_PAYED = (
    'payee_contract_withdraw',
    'payee_balance_proof',

    'payer_contract_withdraw',
    'payer_balance_proof',
)
STATE_TRANSFER_FINAL = (
    'payee_contract_withdraw',
    'payee_balance_proof',
    'payee_expired',

    'payer_contract_withdraw',
    'payer_balance_proof',
    'payer_expired',
)


def is_lock_valid(block_number, transfer):
    """ True if the lock has not expired. """
    return block_number <= transfer.expiration


def is_safe_to_wait(block_number, transfer, reveal_timeout):
    """ True if there are more than enough blocks to safely settle on chain and
    waiting is safe.
    """
    # A node may wait for a new balance proof while there are reveal_timeout
    # left, at that block and onwards it is not safe to wait.
    return block_number < transfer.expiration - reveal_timeout


def is_valid_refund(original_transfer, refund_sender, refund_transfer):
    """ True if the refund transfer matches the original transfer. """
    # Ignore a refund from the target
    if refund_sender == original_transfer.target:
        return False

    return (
        original_transfer.identifier == refund_transfer.identifier and
        original_transfer.amount == refund_transfer.amount and
        original_transfer.hashlock == refund_transfer.hashlock and
        original_transfer.target == refund_transfer.target and

        # A larger-or-equal expiration is byzantine behavior that favors this
        # node, neverthless it's being ignored since the only reason for the
        # other node to use an invalid expiration is to play the protocol.
        original_transfer.expiration > refund_transfer.expiration
    )


def get_pending_transfer_pairs(transfers_pair):
    """ Return the transfer pairs that han't reached a final state. """
    pending_pairs = list(
        pair
        for pair in transfers_pair
        if pair.payee_state not in STATE_TRANSFER_FINAL or
        pair.payer_state not in STATE_TRANSFER_FINAL
    )
    return pending_pairs


def get_timeout_blocks(payer_route, payer_transfer, block_number):
    """ Return the timeout blocks, it's the base value from which the payee's
    lock timeout must be computed.

    The payee lock timeout is crucial for safety of the mediate transfer, the
    value must be choosen so that the payee hop is forced to reveal the secret
    with sufficient time for this node to claim the received lock from the
    payer hop.

    The timeout blocks must be the smallest of:

    - payer_transfer.expiration: The payer lock expiration, to force the payee
      to reveal the secret before the lock expires.
    - payer_route.settle_timeout: Lock expiration must be lower than
      the settlement period since the lock cannot be claimed after the channel is
      settled.
    - payer_route.close_block: If the block is closed the settlement period is
      running and the lock expiration must be lower than number of blocks left.
    """
    blocks_until_settlement = payer_route.settle_timeout

    if payer_route.close_block is not None:
        assert block_number >= payer_route.close_block

        elapsed_blocks = block_number - payer_route.close_block
        blocks_until_settlement -= elapsed_blocks

    safe_payer_timeout = min(
        blocks_until_settlement,
        payer_transfer.expiration - block_number,
    )
    timeout_blocks = safe_payer_timeout - TRANSIT_BLOCKS

    return timeout_blocks


def clear_if_finalized(iteration):
    """ Clear the state if all transfer pairs have finalized. """
    state = iteration.new_state

    all_finalized = all(
        pair.payee_state in STATE_TRANSFER_FINAL and pair.payer_state in STATE_TRANSFER_FINAL
        for pair in state.transfers_pair
    )

    # TODO: how do we define success and failure for a mediator since the state
    # of individual paths may differ?

    if all_finalized:
        return Iteration(None, iteration.events)
    return iteration


def next_route(routes_state, timeout_blocks, transfer_amount):
    """ Finds the route first route available that can be used.

    Args:
        routes_state (RoutesState): The route states to do the search, it's
            assume thet available_routes is ordered from best to worst route.
        timeout_blocks (int): Base number of available blocks used to compute
            the lock timeout.
        transfer_amount (int): The amount of tokens that will be transferred
            through the given route.

    Returns:
        (RouteState): The next route.
    """
    while routes_state.available_routes:
        route = routes_state.available_routes.pop(0)

        lock_timeout = timeout_blocks - route.reveal_timeout
        enough_balance = route.available_balance >= transfer_amount

        if enough_balance and lock_timeout > 0:
            return route
        else:
            routes_state.ignored_routes.append(route)

    return None


def next_transfer_pair(payer_route, payer_transfer, routes_state, timeout_blocks, block_number):
    """ Given a mediation payer route tries a new route to proceed with the
    mediation.

    Args:
        payer_route (RouteState): The previous route in the path that provides
            the token for the mediation.
        payer_transfer (LockedTransferState): The transfer received from the
            payer_route.
        routes_state (RoutesState): The route states to do the search, it's
            assume thet available_routes is ordered from best to worst route.
        timeout_blocks (int): Base number of available blocks used to compute
            the lock timeout.
        block_number (int): The current block number.
    """
    assert timeout_blocks > 0
    assert timeout_blocks <= payer_transfer.expiration - block_number

    transfer_pair = None
    mediated_events = list()

    payee_route = next_route(
        routes_state,
        timeout_blocks,
        payer_transfer.amount,
    )

    if payee_route:
        assert payee_route.reveal_timeout < timeout_blocks

        lock_timeout = timeout_blocks - payee_route.reveal_timeout
        lock_expiration = lock_timeout + block_number

        payee_transfer = LockedTransferState(
            payer_transfer.identifier,
            payer_transfer.amount,
            payer_transfer.token,
            payer_transfer.target,
            lock_expiration,
            payer_transfer.hashlock,
            payer_transfer.secret,
        )

        transfer_pair = MediationPairState(
            payer_route,
            payer_transfer,
            payee_route,
            payee_transfer,
        )

        mediated_events = [
            mediatedtransfer(payee_transfer, payer_route.node_address),
        ]

    return (
        transfer_pair,
        mediated_events,
    )


def set_secret(state, secret):
    """ Set the secret to all mediated transfers.

    It doesnt matter if the secret was learned through the blockchain or a
    secret reveal message.

    Note:
        `state` is changed in place.
    """
    state.secret = secret

    for pair in state.transfers_pair:
        pair.payer_transfer.secret = secret
        pair.payee_transfer.secret = secret


def set_payee_state_and_check_reveal_order(transfers_pair,  # pylint: disable=invalid-name
                                           payee_address,
                                           new_payee_state):
    """ Set the state of a transfer *sent* to a payee and check the secret is
    being revealed backwards.

    Note:
        the elements from transfers_pair are changed in place, the list must
        contain all the know transfers to properly check reveal order.
    """
    assert new_payee_state in MediationPairState.valid_payee_states

    wrong_reveal_order = False
    for back in reversed(transfers_pair):
        if back.payee_route.node_address == payee_address:
            back.payee_state = new_payee_state
            break

        elif back.payee_state not in STATE_SECRET_KNOWN:
            wrong_reveal_order = True

    if wrong_reveal_order:
        # TODO: append an event for byzantine behavior
        return list()

    return list()


def events_for_refund_transfer(refund_route, refund_transfer, timeout_blocks, block_number):
    """ Refund the transfer.

    Args:
        refund_route (RouteState): The original route that sent the mediated
            transfer to this node.
        refund_transfer (LockedTransferState): The original mediated transfer
            from the refund_route.
        timeout_blocks (int): The number of blocks available from the /latest
            transfer/ received by this node, this transfer might be the
            refund_transfer (if no route was available) or a refund transfer from a
            down stream node.
        block_number (int): The current block number.

    Returns:
        An empty list if there are not enough blocks to safely create a refund,
        or a list with an refund event.
    """
    # A refund transfer works like a special SendMediatedTransfer, so it must
    # follow the same rules and decrement reveal_timeout from the
    # payee_transfer.
    new_lock_timeout = timeout_blocks - refund_route.reveal_timeout

    if new_lock_timeout > 0:
        new_lock_expiration = new_lock_timeout + block_number

        refund_transfer = SendRefundTransfer(
            refund_transfer.identifier,
            refund_transfer.token,
            refund_transfer.amount,
            refund_transfer.hashlock,
            new_lock_expiration,
            refund_route.node_address,
        )

        return [refund_transfer]

    # Can not create a refund lock with a safe expiration, so don't do anything
    # and wait for the received lock to expire.
    return list()


def events_for_revealsecret(state):
    """ Reveal the secret backwards.

    This node is named N, suppose there is a mediated transfer with two
    refund transfers, one from B and one from C:

        A-N-B...B-N-C..C-N-D

    Under normal operation this will first learn the secret from D, then
    reveal to C, wait for C to tell us that it knows the secret then reveal
    it to B, and again wait for B before revealing the secret to A.

    If B somehow sent a reveal secret before C and D, then the secret will be
    revealed to A, but not C and D, meaning the secret won't be propagate
    forward.

    If B and D sent a reveal secret at about the same time, the secret will
    only be revealed to B upon confirmation from C. B should not have learnt
    the secret before time but since it knows it may withdraw on-chain, so N
    needs to proceed with the protol backwards to stay even.
    """

    events = list()
    for pair in reversed(state.transfers_pair):
        payee_secret = pair.payee_transfer.state in STATE_SECRET_KNOWN
        payer_secret = pair.payer_transfer.state in STATE_SECRET_KNOWN

        if payee_secret and not payer_secret:
            pair.payer_transfer.state = 'payer_secret_revealed'
            reveal_secret = SendRevealSecret(
                pair.payer_transfer.identifier,
                pair.payer_transfer.secret,
                pair.payer_route.node_address,
                state.our_address,
            )
            events.append(reveal_secret)

    return events


def events_for_balanceproof(state):
    """ Send the balance proof to nodes that know the secret. """

    events = list()
    for pair in reversed(state.transfers_pair):
        payee_knows_secret = pair.payee_transfer.state in STATE_SECRET_KNOWN
        payee_payed = pair.payee_transfer.state in STATE_TRANSFER_PAYED
        lock_valid = is_lock_valid(state.block_number, pair.payee_transfer)

        if payee_knows_secret and not payee_payed and lock_valid:
            pair.payee_transfer.state = 'payee_balance_proof'
            balance_proof = SendBalanceProof(
                pair.payee_transfer.identifier,
                pair.payee_route.node_address,
            )
            events.append(balance_proof)

    return events


def secret_learned(state, secret, payee_address, new_payee_state):
    """ Set the state of the `payee_address` transfer, check the secret is
    being revealed backwards, and if necessary send out RevealSecret and
    BalanceProof.
    """
    assert new_payee_state in STATE_SECRET_KNOWN

    # TODO: if any of the transfers is in expired state, event for byzantine
    # behavior

    if state.secret is None:
        set_secret(state, secret)

    # change the payee state
    wrong_order = set_payee_state_and_check_reveal_order(
        state.transfers_pair,
        payee_address,
        new_payee_state,
    )

    # reveal the secret backwards
    secret_reveal = events_for_revealsecret(
        state
    )

    # send the balance proof to payee that knows the secret but is not payed
    # yet
    balance_proof = events_for_balanceproof(
        state
    )

    iteration = Iteration(
        state,
        wrong_order + secret_reveal + balance_proof,
    )

    return iteration


def mediate_transfer(state, payer_route, payer_transfer):
    transfer_pair = None
    mediated_events = list()

    timeout_blocks = get_timeout_blocks(
        payer_route,
        payer_transfer,
        state.block_number,
    )

    if timeout_blocks > 0:
        transfer_pair, mediated_events = next_transfer_pair(
            payer_route,
            payer_transfer,
            state.routes,
            timeout_blocks,
            state.block_number,
        )

    if transfer_pair is None:
        if state.transfers_pair:
            original_transfer = state.transfers_pair[0].payer_transfer
            original_route = state.transfers_pair[0].payer_route
        else:
            original_route = payer_route
            original_transfer = payer_transfer

        refund_events = events_for_refund_transfer(
            original_route,
            original_transfer,
            timeout_blocks,
            state.block_number,
        )

        iteration = Iteration(state, refund_events)

    else:
        # the list must be ordered from high to low expiration, expiration
        # handling depends on it
        state.transfers_pair.append(transfer_pair)
        iteration = Iteration(state, mediated_events)

    return iteration


def handle_block(state, state_change):
    """ After Raiden learns about a new block this function must be called to
    handle expiration of the hash time locks.

    Args:
        state (MediatorState): The current state.

    Return:
        Iteration: The resulting iteration
    """
    block_number = state_change.block_number
    state.block_number = block_number

    events = list()
    pending_transfers_pairs = get_pending_transfer_pairs(state.transfers_pair)

    for pair in reversed(pending_transfers_pairs):
        # Only withdraw on chain if the corresponding payee transfer is payed,
        # this prevents attacks were tokens are burned to force a channel close.
        payee_payed = pair.payee_transfer.state in STATE_TRANSFER_PAYED
        payer_payed = pair.payer_transfer.state in STATE_TRANSFER_PAYED
        witdrawing = pair.payer_state == 'payer_waiting_withdraw'

        if payee_payed and not payer_payed and not witdrawing:
            safe_to_wait = is_safe_to_wait(
                block_number,
                pair.payer_transfer,
                pair.payer_route.reveal_timeout,
            )

            if not safe_to_wait:
                pair.payer_state = 'payer_waiting_withdraw'
                settle_channel = ContractSendWithdraw(
                    pair.payer_transfer,
                    pair.payer_route.channel_address,
                )
                events.append(settle_channel)

        if pair.payer_transfer.expiration > block_number:
            assert pair.payee_state not in STATE_TRANSFER_PAYED
            pair.payee_state = 'payee_expired'
            pair.payer_state = 'payer_expired'

    iteration = Iteration(state, events)

    return iteration


def handle_refundtransfer(state, state_change):
    """ Validate and handle a ReceiveTransferRefund state change.

    A node might participate in mediated transfer more than once because of
    refund transfers, eg. A-B-C-B-D-T, B tried to mediated the transfer through
    C, which didn't have a available route to proceed and refunds B, at this
    point B is part of the path again and will try a new partner to proceed
    with the mediation through D, D finally reaches the target T.

    In the above scenario B has two pairs of payer and payee transfers:

        payer:A payee:C from the first SendMediatedTransfer
        payer:C payee:D from the following SendRefundTransfer

    Args:
        state (MediatorState): Current state.
        state_change (ReceiveTransferRefund): The state change.

    Returns:
        Iteration: The resulting iteration.
    """
    assert state.secret is None, 'refunds are not allowed if the secret is revealed'

    # The last sent transfer is the only one thay may be refunded, all the
    # previous ones are refunded already.
    transfer_pair = state.transfers_pair[-1]
    payee_transfer = transfer_pair.payee_transfer

    if is_valid_refund(payee_transfer, state_change.sender, state_change.transfer):
        payer_route = transfer_pair.payee_route
        payer_transfer = state_change.transfer
        state.routes.refund_routes.append(payer_route)
        iteration = mediate_transfer(
            state,
            payer_route,
            payer_transfer,
        )

    else:
        # TODO: Use an event to notify about byzantine behavior
        iteration = Iteration(state, list())

    return iteration


def handle_secretreveal(state, state_change):
    """ Validate and handle a ReceiveSecretReveal state change.

    The Secret must propagate backwards through the chain of mediators, this
    function will record the learned secret, check if the secret is propagating
    backwards (for the known paths), and send the BalanceProof/RevealSecret if
    necessary.
    """
    secret = state_change.secret

    if sha3(secret) == state.hashlock:
        iteration = secret_learned(
            state,
            secret,
            state_change.sender,
            'payee_secret_revealed',
        )

    else:
        # TODO: event for byzantine behavior
        iteration = Iteration(state, list())

    return iteration


def handle_contractwithdraw(state, state_change):
    """ Handle a NettingChannelUnlock state change. """
    assert sha3(state.secret) == state.hashlock, 'secret must be validated by the smart contract'

    for pair in state.transfers_pair:
        if pair.payer_route.channel_address == state_change.channel_address:
            pair.payer_state = 'payer_contract_withdraw'
            break
    else:
        iteration = secret_learned(
            state,
            state_change.secret,
            state_change.sender,
            'payee_contract_withdraw',
        )

    return iteration


def handle_balanceproof(state, state_change):
    """ Handle a ReceiveBalanceProof state change. """
    for pair in state.transfers_pair:
        if pair.payer_route.channel_address == state_change.node_address:
            pair.payer_state = 'payer_balance_proof'

    iteration = Iteration(state, list())

    return iteration


def handle_routechange(state, state_change):
    """ Hande a ActionRouteChange state change. """
    update_route(state, state_change)
    iteration = Iteration(state, list())
    return iteration


def state_transition(state, state_change):
    """ State machine for a node mediating a transfer. """
    # pylint: disable=too-many-branches
    # Notes:
    # - A user cannot cancel a mediated transfer after it was initiated, she
    #   may only reject to mediate before hand. This is because the mediator
    #   doesn't control the secret reveal and needs to wait for the lock
    #   expiration before safely discarding the transfer.

    if state is None:
        if isinstance(state_change, ActionInitMediator):
            routes = state_change.routes

            from_route = state_change.from_route
            from_transfer = state_change.from_transfer

            state = MediatorState(
                state_change.our_address,
                routes,
                state_change.block_number,
                from_transfer.hashlock,
            )

            iteration = mediate_transfer(state, from_route, from_transfer)

    elif state.secret is None:
        if isinstance(state_change, Block):
            iteration = handle_block(state, state_change)

        elif isinstance(state_change, ActionRouteChange):
            iteration = handle_routechange(state, state_change)

        elif isinstance(state_change, ReceiveTransferRefund):
            iteration = handle_refundtransfer(state, state_change)

        elif isinstance(state_change, ReceiveSecretReveal):
            iteration = handle_secretreveal(state, state_change)

        elif isinstance(state_change, ContractReceiveWithdraw):
            iteration = handle_contractwithdraw(state, state_change)

    else:
        if isinstance(state_change, Block):
            iteration = handle_block(state, state_change)

        if isinstance(state_change, ReceiveSecretReveal):
            iteration = handle_secretreveal(state, state_change)

        elif isinstance(state_change, ReceiveBalanceProof):
            iteration = handle_balanceproof(state, state_change)

        elif isinstance(state_change, ContractReceiveWithdraw):
            iteration = handle_contractwithdraw(state, state_change)

    return clear_if_finalized(iteration)
