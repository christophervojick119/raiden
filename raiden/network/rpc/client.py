# -*- coding: utf-8 -*-
import warnings
import time
import os
from binascii import unhexlify
from typing import Optional, Union
from json.decoder import JSONDecodeError

from web3 import Web3, HTTPProvider
from web3.middleware import geth_poa_middleware
from eth_utils import to_checksum_address
import gevent
import cachetools
from eth_abi import encode_abi
from web3.utils.abi import get_constructor_abi, get_abi_input_types
from gevent.lock import Semaphore
import structlog

from raiden.exceptions import (
    AddressWithoutCode,
    EthNodeCommunicationError,
    RaidenShuttingDown,
)
from raiden.network.rpc.smartcontract_proxy import ContractProxy
from raiden.settings import GAS_PRICE, GAS_LIMIT, RPC_CACHE_TTL
from raiden.utils import (
    address_encoder,
    data_encoder,
    privatekey_to_address,
    quantity_decoder,
    quantity_encoder,
    encode_hex,
)
from raiden.utils.typing import Address
from raiden.utils.solc import (
    solidity_unresolved_symbols,
    solidity_library_symbol,
    solidity_resolve_symbols
)

log = structlog.get_logger(__name__)  # pylint: disable=invalid-name


def make_connection_test_middleware(client):
    def connection_test_middleware(make_request, web3):
        """ Creates middleware that checks if the provider is connected. """

        # not sure why this is necessary, but otherwise the first rpc call fails
        web3.providers[0].make_request('web3_clientVersion', [])

        def middleware(method, params):
            # raise exception when shutting down
            if client.stop_event and client.stop_event.is_set():
                raise RaidenShuttingDown()

            try:
                if web3.isConnected():
                    return make_request(method, params)
                else:
                    raise EthNodeCommunicationError('Web3 provider not connected')

            # the isConnected check doesn't currently catch JSON errors
            # see https://github.com/ethereum/web3.py/issues/866
            except JSONDecodeError:
                raise EthNodeCommunicationError('Web3 provider not connected')
            except EthNodeCommunicationError:
                raise EthNodeCommunicationError('Web3 provider not connected')

        return middleware
    return connection_test_middleware


def check_address_has_code(
        client: 'JSONRPCClient',
        address: Address,
        contract_name: str = ''):
    """ Checks that the given address contains code. """
    result = client.web3.eth.getCode(to_checksum_address(address), 'latest')

    if not result:
        if contract_name:
            formated_contract_name = '[{}]: '.format(contract_name)
        else:
            formated_contract_name = ''

        raise AddressWithoutCode('{}Address {} does not contain code'.format(
            formated_contract_name,
            address_encoder(address),
        ))


def deploy_dependencies_symbols(all_contract):
    dependencies = {}

    symbols_to_contract = dict()
    for contract_name in all_contract:
        symbol = solidity_library_symbol(contract_name)

        if symbol in symbols_to_contract:
            raise ValueError('Conflicting library names.')

        symbols_to_contract[symbol] = contract_name

    for contract_name, contract in all_contract.items():
        unresolved_symbols = solidity_unresolved_symbols(contract['bin'])
        dependencies[contract_name] = [
            symbols_to_contract[unresolved]
            for unresolved in unresolved_symbols
        ]

    return dependencies


def dependencies_order_of_build(target_contract, dependencies_map):
    """ Return an ordered list of contracts that is sufficient to sucessfully
    deploy the target contract.

    Note:
        This function assumes that the `dependencies_map` is an acyclic graph.
    """
    if not dependencies_map:
        return [target_contract]

    if target_contract not in dependencies_map:
        raise ValueError('no dependencies defined for {}'.format(target_contract))

    order = [target_contract]
    todo = list(dependencies_map[target_contract])

    while todo:
        target_contract = todo.pop(0)
        target_pos = len(order)

        for dependency in dependencies_map[target_contract]:
            # we need to add the current contract before all its depedencies
            if dependency in order:
                target_pos = order.index(dependency)
            else:
                todo.append(dependency)

        order.insert(target_pos, target_contract)

    order.reverse()
    return order


def format_data_for_rpccall(
        sender: Address = b'',
        to: Address = b'',
        value: int = 0,
        data: bytes = b'',
        startgas: int = GAS_LIMIT,
        gasprice: int = GAS_PRICE):
    """ Helper to format the transaction data. """

    return {
        'from': to_checksum_address(sender),
        'to': to_checksum_address(to),
        'value': quantity_encoder(value),
        'gasPrice': quantity_encoder(gasprice),
        'gas': quantity_encoder(startgas),
        'data': data_encoder(data)
    }


class JSONRPCClient:
    """ Ethereum JSON RPC client.

    Args:
        host: Ethereum node host address.
        port: Ethereum node port number.
        privkey: Local user private key, used to sign transactions.
        nonce_update_interval: Update the account nonce every
            `nonce_update_interval` seconds.
        nonce_offset: Network's default base nonce number.
    """

    def __init__(
            self,
            host: str,
            port: int,
            privkey: bytes,
            gasprice: int = None,
            nonce_update_interval: float = 5.0,
            nonce_offset: int = 0):

        if privkey is None or len(privkey) != 32:
            raise ValueError('Invalid private key')

        endpoint = 'http://{}:{}'.format(host, port)

        self.port = port
        self.privkey = privkey
        self.sender = privatekey_to_address(privkey)
        # Needs to be initialized to None in the beginning since JSONRPCClient
        # gets constructed before the RaidenService Object.
        self.stop_event = None

        self.nonce_last_update = 0
        self.nonce_available_value = None
        self.nonce_lock = Semaphore()
        self.nonce_update_interval = nonce_update_interval
        self.nonce_offset = nonce_offset
        self.given_gas_price = gasprice

        cache = cachetools.TTLCache(
            maxsize=1,
            ttl=RPC_CACHE_TTL,
        )
        cache_wrapper = cachetools.cached(cache=cache)
        self.gaslimit = cache_wrapper(self._gaslimit)
        cache = cachetools.TTLCache(
            maxsize=1,
            ttl=RPC_CACHE_TTL,
        )
        cache_wrapper = cachetools.cached(cache=cache)
        self.gasprice = cache_wrapper(self._gasprice)

        # web3
        self.web3: Web3 = Web3(HTTPProvider(endpoint))
        # we use a PoA chain for smoketest, use this middleware to fix this
        self.web3.middleware_stack.inject(geth_poa_middleware, layer=0)

        # create the connection test middleware
        connection_test = make_connection_test_middleware(self)
        self.web3.middleware_stack.inject(connection_test, layer=0)

    def __repr__(self):
        return '<JSONRPCClient @%d>' % self.port

    def block_number(self):
        """ Return the most recent block. """
        return self.web3.eth.blockNumber

    def nonce_needs_update(self):
        if self.nonce_available_value is None:
            return True

        now = time.time()

        # Python's 2.7 time is not monotonic and it's affected by clock resets,
        # force an update.
        if self.nonce_last_update > now:
            return True

        return now - self.nonce_last_update > self.nonce_update_interval

    def nonce_update_from_node(self):
        nonce = -2
        nonce_available_value = self.nonce_available_value or -1

        # Wait until all tx are registered as pending
        while nonce < nonce_available_value:
            pending_transactions = self.web3.eth.getTransactionCount(
                to_checksum_address(self.sender)
            )
            nonce = pending_transactions + self.nonce_offset

            log.debug(
                'updated nonce from server',
                server=nonce,
                local=nonce_available_value,
            )

        self.nonce_last_update = time.time()
        self.nonce_available_value = nonce

    def nonce(self):
        with self.nonce_lock:
            if self.nonce_needs_update():
                self.nonce_update_from_node()

            self.nonce_available_value += 1
            return self.nonce_available_value - 1

    def inject_stop_event(self, event):
        self.stop_event = event

    def balance(self, account: Address):
        """ Return the balance of the account of given address. """
        return self.web3.eth.getBalance(to_checksum_address(account), 'pending')

    def _gaslimit(self, location='pending') -> int:
        gas_limit = self.web3.eth.getBlock(location)['gasLimit']
        return gas_limit * 8 // 10

    def _gasprice(self) -> int:
        if self.given_gas_price:
            return self.given_gas_price

        return self.web3.eth.gasPrice

    def check_startgas(self, startgas):
        if not startgas:
            return self.gaslimit()
        return startgas

    def new_contract_proxy(self, contract_interface, contract_address: Address):
        """ Return a proxy for interacting with a smart contract.

        Args:
            contract_interface: The contract interface as defined by the json.
            address: The contract's address.
        """
        return ContractProxy(
            self.sender,
            contract_interface,
            contract_address,
            self.eth_call,
            self.send_transaction,
            self.eth_estimateGas,
        )

    def deploy_solidity_contract(
            self,  # pylint: disable=too-many-locals
            contract_name,
            all_contracts,
            libraries,
            constructor_parameters,
            contract_path=None,
            timeout=None):
        """
        Deploy a solidity contract.
        Args:
            sender (address): the sender address
            contract_name (str): the name of the contract to compile
            all_contracts (dict): the json dictionary containing the result of compiling a file
            libraries (list): A list of libraries to use in deployment
            constructor_parameters (tuple): A tuple of arguments to pass to the constructor
            contract_path (str): If we are dealing with solc >= v0.4.9 then the path
                                 to the contract is a required argument to extract
                                 the contract data from the `all_contracts` dict.
            timeout (int): Amount of time to poll the chain to confirm deployment
        """
        if contract_name in all_contracts:
            contract_key = contract_name

        elif contract_path is not None:
            contract_key = os.path.basename(contract_path) + ':' + contract_name

            if contract_key not in all_contracts:
                raise ValueError('Unknown contract {}'.format(contract_name))
        else:
            raise ValueError(
                'Unknown contract {} and no contract_path given'.format(contract_name)
            )

        libraries = dict(libraries)
        contract = all_contracts[contract_key]
        contract_interface = contract['abi']
        symbols = solidity_unresolved_symbols(contract['bin'])

        if symbols:
            available_symbols = list(map(solidity_library_symbol, all_contracts.keys()))

            unknown_symbols = set(symbols) - set(available_symbols)
            if unknown_symbols:
                msg = 'Cannot deploy contract, known symbols {}, unresolved symbols {}.'.format(
                    available_symbols,
                    unknown_symbols,
                )
                raise Exception(msg)

            dependencies = deploy_dependencies_symbols(all_contracts)
            deployment_order = dependencies_order_of_build(contract_key, dependencies)

            deployment_order.pop()  # remove `contract_name` from the list

            log.debug('Deploying dependencies: {}'.format(str(deployment_order)))

            for deploy_contract in deployment_order:
                dependency_contract = all_contracts[deploy_contract]

                hex_bytecode = solidity_resolve_symbols(dependency_contract['bin'], libraries)
                bytecode = unhexlify(hex_bytecode)

                dependency_contract['bin'] = bytecode

                transaction_hash_hex = self.send_transaction(
                    to=Address(b''),
                    data=bytecode,
                )
                transaction_hash = unhexlify(transaction_hash_hex)

                self.poll(transaction_hash, timeout=timeout)
                receipt = self.web3.eth.getTransactionReceipt(transaction_hash)

                contract_address = receipt['contractAddress']
                # remove the hexadecimal prefix 0x from the address
                contract_address = contract_address[2:]

                libraries[deploy_contract] = contract_address

                deployed_code = self.web3.eth.getCode(to_checksum_address(contract_address))

                if not deployed_code:
                    raise RuntimeError('Contract address has no code, check gas usage.')

            hex_bytecode = solidity_resolve_symbols(contract['bin'], libraries)
            bytecode = unhexlify(hex_bytecode)

            contract['bin'] = bytecode

        if isinstance(contract['bin'], str):
            contract['bin'] = unhexlify(contract['bin'])

        if constructor_parameters:
            constructor_abi = get_constructor_abi(contract_interface)
            constructor_types = get_abi_input_types(constructor_abi)
            parameters = encode_abi(constructor_types, constructor_parameters)
            bytecode = contract['bin'] + parameters
        else:
            bytecode = contract['bin']

        transaction_hash_hex = self.send_transaction(
            to=Address(b''),
            data=bytecode,
        )
        transaction_hash = unhexlify(transaction_hash_hex)

        self.poll(transaction_hash, timeout=timeout)
        receipt = self.web3.eth.getTransactionReceipt(transaction_hash)
        contract_address = receipt['contractAddress']

        deployed_code = self.web3.eth.getCode(to_checksum_address(contract_address))

        if not deployed_code:
            raise RuntimeError(
                'Deployment of {} failed. Contract address has no code, check gas usage.'.format(
                    contract_name,
                )
            )

        return self.new_contract_proxy(
            contract_interface,
            contract_address,
        )

    def send_transaction(
            self,
            to: Address,
            value: int = 0,
            data: bytes = b'',
            startgas: int = None,
    ):
        """ Helper to send signed messages.

        This method will use the `privkey` provided in the constructor to
        locally sign the transaction. This requires an extended server
        implementation that accepts the variables v, r, and s.
        """

        if to == b'' and data.isalnum():
            warnings.warn(
                'Verify that the data parameter is _not_ hex encoded, if this is the case '
                'the data will be double encoded and result in unexpected '
                'behavior.'
            )

        if to == b'0' * 20:
            warnings.warn('For contract creation the empty string must be used.')

        transaction = dict(
            nonce=self.nonce(),
            gasPrice=self.gasprice(),
            gas=self.check_startgas(startgas),
            value=value,
            data=data
        )

        # add the to address if not deploying a contract
        if to != b'':
            transaction['to'] = to_checksum_address(to)

        signed_txn = self.web3.eth.account.signTransaction(transaction, self.privkey)

        result = self.web3.eth.sendRawTransaction(signed_txn.rawTransaction)
        encoded_result = encode_hex(result)
        return encoded_result[2 if encoded_result.startswith('0x') else 0:]

    def eth_call(
            self,
            sender: Address = b'',
            to: Address = b'',
            value: int = 0,
            data: bytes = b'',
            startgas: int = None,
            block_number: Union[str, int] = 'latest'
    ) -> bytes:
        """ Executes a new message call immediately without creating a
        transaction on the blockchain.

        Args:
            sender: The address the transaction is sent from.
            to: The address the transaction is directed to.
            gas: Gas provided for the transaction execution. eth_call
                consumes zero gas, but this parameter may be needed by some
                executions.
            gasPrice: gasPrice used for unit of gas paid.
            value: Integer of the value sent with this transaction.
            data: Hash of the method signature and encoded parameters.
                For details see Ethereum Contract ABI.
            block_number: Determines the state of ethereum used in the
                call.
        """
        startgas = self.check_startgas(startgas)
        json_data = format_data_for_rpccall(
            sender,
            to,
            value,
            data,
            startgas,
            self.gasprice(),
        )
        return self.web3.eth.call(json_data, block_number)

    def eth_estimateGas(
            self,
            sender: Address = b'',
            to: Address = b'',
            value: int = 0,
            data: bytes = b'',
            startgas: int = None
    ) -> Optional[int]:
        """ Makes a call or transaction, which won't be added to the blockchain
        and returns the used gas, which can be used for estimating the used
        gas.

        Args:
            sender: The address the transaction is sent from.
            to: The address the transaction is directed to.
            gas: Gas provided for the transaction execution. eth_call
                consumes zero gas, but this parameter may be needed by some
                executions.
            value: Integer of the value sent with this transaction.
            data: Hash of the method signature and encoded parameters.
                For details see Ethereum Contract ABI.
            block_number: Determines the state of ethereum used in the
                call.
        """
        startgas = self.check_startgas(startgas)
        json_data = format_data_for_rpccall(
            sender,
            to,
            value,
            data,
            startgas
        )
        try:
            return self.web3.eth.estimateGas(json_data)
        except ValueError as err:
            print(err)
            tx_would_fail = (
                '-32015' in str(err) or
                '-32000' in str(err)
            )
            # tx_would_fail = e.error_code and e.error_code in (-32015, -32000)
            if tx_would_fail:  # -32015 is parity and -32000 is geth
                return None
            else:
                raise err

    def poll(
            self,
            transaction_hash: bytes,
            confirmations: int = None,
            timeout: float = None):
        """ Wait until the `transaction_hash` is applied or rejected.
        If timeout is None, this could wait indefinitely!

        Args:
            transaction_hash: Transaction hash that we are waiting for.
            confirmations: Number of block confirmations that we will
                wait for.
            timeout: Timeout in seconds, raise an Excpetion on timeout.
        """
        if transaction_hash.startswith(b'0x'):
            warnings.warn(
                'transaction_hash seems to be already encoded, this will'
                ' result in unexpected behavior'
            )

        if len(transaction_hash) != 32:
            raise ValueError(
                'transaction_hash length must be 32 (it might be hex encoded)'
            )

        transaction_hash = data_encoder(transaction_hash)

        deadline = None
        if timeout:
            deadline = gevent.Timeout(timeout)
            deadline.start()

        try:
            # used to check if the transaction was removed, this could happen
            # if gas price is too low:
            #
            # > Transaction (acbca3d6) below gas price (tx=1 Wei ask=18
            # > Shannon). All sequential txs from this address(7d0eae79)
            # > will be ignored
            #
            last_result = None

            while True:
                # Could return None for a short period of time, until the
                # transaction is added to the pool
                transaction = self.web3.eth.getTransaction(transaction_hash)

                # if the transaction was added to the pool and then removed
                if transaction is None and last_result is not None:
                    raise Exception('invalid transaction, check gas price')

                # the transaction was added to the pool and mined
                if transaction and transaction['blockNumber'] is not None:
                    break

                last_result = transaction

                gevent.sleep(.5)

            if confirmations:
                # this will wait for both APPLIED and REVERTED transactions
                transaction_block = quantity_decoder(transaction['blockNumber'])
                confirmation_block = transaction_block + confirmations

                block_number = self.block_number()

                while block_number < confirmation_block:
                    gevent.sleep(.5)
                    block_number = self.block_number()

        except gevent.Timeout:
            raise Exception('timeout when polling for transaction')

        finally:
            if deadline:
                deadline.cancel()
