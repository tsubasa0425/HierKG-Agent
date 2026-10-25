import type { BuildStreamCallbacks, BuildStreamEvent } from './buildApi';

const STALL_MS = 45_000; // 服务端每 15s 心跳；45s 无帧判定断线

/**
 * 订阅构建/导入任务 SSE。
 * @param after 上次收到的 seq；首次连接传 undefined 以获取 snapshot。
 * @returns AbortController，卸载/切换任务时调用 abort()。
 */
export function streamBuildJob(
  jobId: string,
  after: number | undefined,
  callbacks: BuildStreamCallbacks,
): AbortController {
  const controller = new AbortController();
  let finished = false;
  let lastEventAt = Date.now();

  const fail = (message: string) => {
    if (finished) return;
    finished = true;
    callbacks.onError(message);
  };

  const stallTimer = setInterval(() => {
    if (Date.now() - lastEventAt > STALL_MS && !finished) {
      clearInterval(stallTimer);
      controller.abort();
      fail('长时间没有收到任务进度，连接已断开');
    }
  }, 5000);

  const dispatch = (event: BuildStreamEvent) => {
    lastEventAt = Date.now();
    callbacks.onEvent(event);
  };

  (async () => {
    try {
      const qs = after !== undefined ? `?after=${after}` : '';
      const response = await fetch(`/api/build/jobs/${jobId}/stream${qs}`, {
        headers: { Accept: 'text/event-stream' },
        signal: controller.signal,
      });
      if (!response.ok) {
        fail(`HTTP ${response.status}: ${response.statusText}`);
        return;
      }
      const reader = response.body?.getReader();
      if (!reader) {
        fail('无法读取任务事件流');
        return;
      }

      const decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const blocks = buffer.split('\n\n');
        buffer = blocks.pop() || '';
        for (const block of blocks) {
          if (!block.trim()) continue;
          const data = block
            .split('\n')
            .filter((line) => line.startsWith('data:'))
            .map((line) => line.slice(5).trimStart())
            .join('\n');
          if (!data) continue;
          try {
            dispatch(JSON.parse(data) as BuildStreamEvent);
          } catch {
            // 忽略非法帧
          }
        }
      }
      finished = true;
      callbacks.onClose();
    } catch (err: unknown) {
      if (err instanceof DOMException && err.name === 'AbortError') return;
      fail('任务事件流连接失败');
    } finally {
      clearInterval(stallTimer);
    }
  })();

  return controller;
}
