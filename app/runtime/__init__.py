"""Runtime 模块：调度、任务执行、出站队列、失败留痕。

唯一横切模块，可 import 全部业务模块（禁止 ``analytics/``）。

任务持久化在 ``task`` 表（``worker/queue.py`` 的 ``PersistentTaskSink``）；
调度、出站合并、失败告警与状态存储分别在 ``scheduler/``、``outbox/``、``state.py``。
"""
