"""智能门锁服务层（厂商可替换）。

两层架构（见 plan §15.2 / §16）：
  - LockProvider（纯厂商抽象）+ HxjiotProvider（慧享家）+ ManualProvider（兜底）
  - OrderLockService（懂订单/保洁的编排层，拥有 door_codes）
"""
