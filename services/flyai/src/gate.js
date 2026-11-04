// 并发闸门：REST 和 MCP 共用一个实例。
// 同一调用方（产品 + 用户的摘要）同时只跑一个 CLI，全局最多 globalLimit 个。
// REST 拿不到就立即返回 429（tryAcquire），MCP 排队等一会儿再放弃（acquire）。

export class ConcurrencyGate {
  constructor(globalLimit = 2) {
    this.globalLimit = globalLimit;
    this.activeGlobal = 0;
    this.activeScopes = new Set();
    this.waiters = [];
  }

  canEnter(scope) {
    return !this.activeScopes.has(scope) && this.activeGlobal < this.globalLimit;
  }

  enter(scope) {
    this.activeGlobal += 1;
    this.activeScopes.add(scope);
  }

  tryAcquire(scope) {
    // 排队的人只会卡在自己的调用方或全局满上：这里能进说明全局有空、本调用方空闲，不算插队
    if (!this.canEnter(scope)) return false;
    this.enter(scope);
    return true;
  }

  /** 等到拿到槽返回 true；超时或 signal 取消返回 false。 */
  acquire(scope, { timeoutMs, signal } = {}) {
    if (this.tryAcquire(scope)) return Promise.resolve(true);
    if (signal?.aborted) return Promise.resolve(false);
    return new Promise((resolve) => {
      const waiter = { scope, resolve: null };
      const settle = (acquired) => {
        clearTimeout(timer);
        signal?.removeEventListener("abort", onAbort);
        resolve(acquired);
      };
      const leave = () => {
        const index = this.waiters.indexOf(waiter);
        if (index !== -1) this.waiters.splice(index, 1);
        settle(false);
        // 队首离开后，后面的人可能已经能进了
        this.wake();
      };
      const onAbort = () => leave();
      const timer = setTimeout(leave, timeoutMs);
      signal?.addEventListener("abort", onAbort, { once: true });
      waiter.resolve = () => settle(true);
      this.waiters.push(waiter);
    });
  }

  release(scope) {
    if (!this.activeScopes.delete(scope)) return;
    this.activeGlobal = Math.max(0, this.activeGlobal - 1);
    this.wake();
  }

  wake() {
    // 按排队顺序放行能进的人；同一调用方的后来者要等前一个结束
    for (let index = 0; index < this.waiters.length && this.activeGlobal < this.globalLimit; ) {
      const waiter = this.waiters[index];
      if (!this.canEnter(waiter.scope)) {
        index += 1;
        continue;
      }
      this.waiters.splice(index, 1);
      this.enter(waiter.scope);
      waiter.resolve();
    }
  }
}
