from __future__ import annotations

from abc import ABC, abstractmethod

from privaite.pii.entity import PIIEntity


class PIIDetector(ABC):
    @abstractmethod
    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
        ...

    @abstractmethod
    async def initialize(self) -> None:
        ...

    async def shutdown(self) -> None:
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        ...
