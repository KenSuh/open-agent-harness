// 한국어: start_review 를 한 번 실행하고 터미널 이벤트를 Promise 로 돌려준다 (Installed 의 업데이트 확인용, 쓰기 없음).
import type { ErrorDto, ReviewResultDto, TaskEvent } from "./contract";
import { toErrorDto, type Transport } from "./transport/types";

export function reviewOnce(transport: Transport, repo: string, path: string): Promise<ReviewResultDto> {
  return new Promise((resolve, reject) => {
    let settled = false;
    const end = (fn: () => void) => {
      if (settled) return;
      settled = true;
      fn();
    };
    const onEvent = (event: TaskEvent<ReviewResultDto>) => {
      if (event.event === "done") end(() => resolve(event.data));
      else if (event.event === "failed") end(() => reject(event.data));
      else if (event.event === "cancelled") {
        end(() => reject({ code: "REVIEW_CANCELLED", message: "The review was cancelled.", hint: null } satisfies ErrorDto));
      }
    };
    transport.startReview({ spec: { repo, path } }, onEvent).then(undefined, (error) => end(() => reject(toErrorDto(error))));
  });
}
