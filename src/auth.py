"""[단계 1] 지갑 연동, EIP-712 서명, 아비트럼 테스트넷 USDC 잔고 조회."""

import os
import time
from functools import lru_cache

from dotenv import load_dotenv
from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3

load_dotenv()

# Arbitrum Sepolia testnet
ARB_RPC = os.getenv("ARB_RPC", "https://sepolia-rollup.arbitrum.io/rpc")
CHAIN_ID = int(os.getenv("CHAIN_ID", "421614"))

# USDC on Arbitrum Sepolia
USDC_ADDRESS = os.getenv("USDC_CONTRACT", "0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d")

USDC_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "recipient", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

EIP712_DOMAIN = {
    "name": "edgeX",
    "version": "1",
    "chainId": CHAIN_ID,
}


def build_web3() -> Web3:
    """아비트럼 테스트넷 노드에 연결된 Web3 인스턴스를 반환합니다."""
    w3 = Web3(Web3.HTTPProvider(ARB_RPC, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        raise ConnectionError(f"아비트럼 노드 연결 실패: {ARB_RPC}")
    return w3


def get_usdc_balance(address: str, w3: Web3 | None = None) -> float:
    """지정 지갑의 USDC 잔고를 float(사람이 읽을 수 있는 단위)으로 반환합니다."""
    if w3 is None:
        w3 = build_web3()

    usdc = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_ADDRESS),
        abi=USDC_ABI,
    )
    raw_balance: int = usdc.functions.balanceOf(
        Web3.to_checksum_address(address)
    ).call()
    decimals: int = usdc.functions.decimals().call()
    return raw_balance / (10 ** decimals)


def get_eth_balance(address: str, w3: Web3 | None = None) -> float:
    """지정 지갑의 ETH(네이티브) 잔고를 ETH 단위로 반환합니다."""
    if w3 is None:
        w3 = build_web3()
    wei = w3.eth.get_balance(Web3.to_checksum_address(address))
    return float(w3.from_wei(wei, "ether"))


class EdgeXAuth:
    """개인 키 기반 지갑 인증 및 EIP-712 주문 서명 클래스."""

    def __init__(self, private_key: str | None = None):
        self._private_key = private_key or os.environ["PRIVATE_KEY"]
        self.account = Account.from_key(self._private_key)
        self.wallet_address: str = self.account.address
        self._nonce = 0
        self._w3: Web3 | None = None

    @property
    def w3(self) -> Web3:
        if self._w3 is None:
            self._w3 = build_web3()
        return self._w3

    def get_usdc_balance(self) -> float:
        return get_usdc_balance(self.wallet_address, self.w3)

    def get_eth_balance(self) -> float:
        return get_eth_balance(self.wallet_address, self.w3)

    def _next_nonce(self) -> int:
        self._nonce += 1
        return self._nonce

    def sign_order(self, order_payload: dict) -> dict:
        """EIP-712 typed-data 서명으로 주문 페이로드를 서명합니다."""
        nonce = self._next_nonce()
        timestamp = int(time.time() * 1000)

        typed_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                ],
                "Order": [
                    {"name": "accountId", "type": "string"},
                    {"name": "symbol", "type": "string"},
                    {"name": "side", "type": "string"},
                    {"name": "price", "type": "string"},
                    {"name": "size", "type": "string"},
                    {"name": "nonce", "type": "uint64"},
                    {"name": "timestamp", "type": "uint64"},
                ],
            },
            "primaryType": "Order",
            "domain": EIP712_DOMAIN,
            "message": {
                **order_payload,
                "nonce": nonce,
                "timestamp": timestamp,
            },
        }

        signable = encode_typed_data(full_message=typed_data)
        signed = self.account.sign_message(signable)

        return {
            **order_payload,
            "nonce": nonce,
            "timestamp": timestamp,
            "signature": signed.signature.hex(),
            "signerAddress": self.wallet_address,
        }

    def sign_cancel(self, order_id: str) -> dict:
        """주문 취소 요청에 EIP-712 서명을 첨부합니다."""
        nonce = self._next_nonce()
        timestamp = int(time.time() * 1000)

        typed_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                ],
                "CancelOrder": [
                    {"name": "orderId", "type": "string"},
                    {"name": "nonce", "type": "uint64"},
                    {"name": "timestamp", "type": "uint64"},
                ],
            },
            "primaryType": "CancelOrder",
            "domain": EIP712_DOMAIN,
            "message": {
                "orderId": order_id,
                "nonce": nonce,
                "timestamp": timestamp,
            },
        }

        signable = encode_typed_data(full_message=typed_data)
        signed = self.account.sign_message(signable)

        return {
            "orderId": order_id,
            "nonce": nonce,
            "timestamp": timestamp,
            "signature": signed.signature.hex(),
            "signerAddress": self.wallet_address,
        }

    def get_headers(self) -> dict:
        """REST API 요청에 사용할 인증 헤더를 반환합니다."""
        return {
            "Content-Type": "application/json",
            "X-Account-Id": os.getenv("EDGEX_ACCOUNT_ID", ""),
        }
