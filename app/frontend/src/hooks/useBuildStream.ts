import { useEffect } from 'react';
import { streamBuildJob } from '../services/buildStream';
import { isActiveJob, useBuildStore } from '../stores/buildStore';

const RETRY_DELAYS = [1000, 2000, 5000, 15000];

/**
 * 当前任务处于活动状态时订阅 SSE；断线按 1/2/5/15s 退避重连，
 * 重连带上 lastSeq 避免重复帧。任务终态后不再重连。
 */
export function useBuildStream(): void {
  const jobId = useBuildStore((s) => s.job?.id);
  const active = useBuildStore((s) => isActiveJob(s.job));

  useEffect(() => {
    if (!jobId || !active) return;

    let closed = false;
    let controller: AbortController | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;

    const schedule = () => {
      if (closed) return;
      const state = useBuildStore.getState();
      if (!isActiveJob(state.job)) {
        state.setStreamStatus('closed');
        return;
      }
      const delay = RETRY_DELAYS[Math.min(attempt, RETRY_DELAYS.length - 1)];
      attempt += 1;
      timer = setTimeout(connect, delay);
    };

    const connect = () => {
      if (closed) return;
      const state = useBuildStore.getState();
      const after = state.lastSeq > 0 ? state.lastSeq : undefined;
      controller = streamBuildJob(jobId, after, {
        onEvent: (event) => {
          if (closed) return;
          attempt = 0;
          useBuildStore.getState().applyEvent(event);
        },
        onError: (message) => {
          if (closed) return;
          const current = useBuildStore.getState();
          if (!isActiveJob(current.job)) {
            current.setStreamStatus('closed');
            return;
          }
          current.setStreamStatus('error');
          current.setNotice(message);
          schedule();
        },
        onClose: () => {
          if (closed) return;
          const current = useBuildStore.getState();
          if (!isActiveJob(current.job)) {
            current.setStreamStatus('closed');
            return;
          }
          current.setStreamStatus('connecting');
          schedule();
        },
      });
    };

    connect();

    return () => {
      closed = true;
      if (timer) clearTimeout(timer);
      controller?.abort();
    };
  }, [jobId, active]);
}
