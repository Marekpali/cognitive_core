# src/adapters/base.py

from abc import ABC, abstractmethod
from typing import Callable

class Adapter(ABC):
    """Base adapter"""
    
    @abstractmethod
    async def listen(self, callback: Callable):
        """Listen for signals"""
        pass
