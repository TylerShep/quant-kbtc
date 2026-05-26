"""
Backtester entrypoint.

`Backtester` now points to the contract-price simulator.
Use `SpotBacktester` for legacy BTC-spot modeling.
"""
from backtesting.contract_backtester import ContractBacktester
from backtesting.spot_backtester import SpotBacktester

Backtester = ContractBacktester

__all__ = ["Backtester", "ContractBacktester", "SpotBacktester"]
