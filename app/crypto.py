"""Application-layer encryption for OAuth tokens stored in Datastore."""

from cryptography.fernet import Fernet


class TokenCipher:
    def __init__(self, key: str):
        self._fernet = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        return self._fernet.decrypt(ciphertext.encode()).decode()

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode()
