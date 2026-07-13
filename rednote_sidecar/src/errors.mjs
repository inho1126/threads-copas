const ERROR_DEFINITIONS = Object.freeze({
  RANGE_NOT_SATISFIABLE: Object.freeze({
    status: 416,
    retryable: false,
    message: '요청한 영상 구간을 제공할 수 없습니다.',
  }),
  JOB_NOT_FOUND: Object.freeze({
    status: 404,
    retryable: false,
    message: '다운로드 작업을 찾을 수 없습니다.',
  }),
  JOB_EXPIRED: Object.freeze({
    status: 410,
    retryable: false,
    message: '다운로드 작업이 만료되었습니다.',
  }),
  JOB_CONCURRENCY_LIMIT: Object.freeze({
    status: 429,
    retryable: true,
    message: '동시에 처리할 수 있는 다운로드 작업 수를 초과했습니다.',
  }),
  FRAME_INVALID: Object.freeze({
    status: 422,
    retryable: false,
    message: '대표 장면 이미지가 올바르지 않습니다.',
  }),
  FRAME_TOO_LARGE: Object.freeze({
    status: 413,
    retryable: false,
    message: '대표 장면 이미지가 허용된 크기를 초과했습니다.',
  }),
  FRAME_DUPLICATE: Object.freeze({
    status: 409,
    retryable: false,
    message: '같은 순번의 대표 장면이 이미 저장되었습니다.',
  }),
  FRAME_WRITE_FAILED: Object.freeze({
    status: 500,
    retryable: true,
    message: '대표 장면 이미지를 로컬 폴더에 저장하지 못했습니다.',
  }),
  INVALID_REQUEST: Object.freeze({
    status: 400,
    retryable: false,
    message: '요청 형식이 올바르지 않습니다.',
  }),
  SIDECAR_AUTH_REQUIRED: Object.freeze({
    status: 401,
    retryable: false,
    message: 'RedNote 로컬 사이드카 인증이 필요합니다.',
  }),
  CHROME_LOGIN_REQUIRED: Object.freeze({
    status: 401,
    retryable: false,
    message: 'Google Chrome에서 RedNote 로그인이 필요합니다.',
  }),
  CHROME_PERMISSION_REQUIRED: Object.freeze({
    status: 403,
    retryable: false,
    message: 'Google Chrome 자동화를 위한 Apple Events 권한이 필요합니다.',
  }),
  CHROME_SEARCH_EMPTY: Object.freeze({
    status: 404,
    retryable: true,
    message: 'RedNote 영상 검색 결과를 찾지 못했습니다.',
  }),
  CHROME_SEARCH_TIMEOUT: Object.freeze({
    status: 504,
    retryable: true,
    message: 'Google Chrome의 RedNote 검색 시간이 초과되었습니다.',
  }),
  CHROME_SEARCH_FAILED: Object.freeze({
    status: 502,
    retryable: true,
    message: 'Google Chrome에서 RedNote 검색을 완료하지 못했습니다.',
  }),
  SEARCH_RESULT_NOT_FOUND: Object.freeze({
    status: 404,
    retryable: false,
    message: 'RedNote 검색 결과가 없거나 만료되었습니다.',
  }),
  INVALID_REDNOTE_URL: Object.freeze({
    status: 400,
    retryable: false,
    message: '유효한 RedNote 공개 영상 링크가 아닙니다.',
  }),
  INVALID_ORIGIN: Object.freeze({
    status: 403,
    retryable: false,
    message: '이 로컬 앱에서 보낸 요청만 허용됩니다.',
  }),
  REQUEST_TOO_LARGE: Object.freeze({
    status: 413,
    retryable: false,
    message: '요청 본문이 허용된 크기를 초과했습니다.',
  }),
  REQUEST_BODY_TIMEOUT: Object.freeze({
    status: 408,
    retryable: true,
    message: '요청 본문 전송 시간이 초과되었습니다.',
  }),
  METHOD_NOT_ALLOWED: Object.freeze({
    status: 405,
    retryable: false,
    message: '허용되지 않은 요청 방식입니다.',
  }),
  SESSION_NOT_FOUND: Object.freeze({
    status: 404,
    retryable: false,
    message: '영상 분석 세션이 없거나 만료되었습니다.',
  }),
  FRAMES_INCOMPLETE: Object.freeze({
    status: 409,
    retryable: false,
    message: '대표 장면을 세 장 이상 저장해야 합니다.',
  }),
  FRAMES_IN_PROGRESS: Object.freeze({
    status: 409,
    retryable: true,
    message: '대표 장면 저장이 진행 중입니다. 잠시 후 다시 시도해 주세요.',
  }),
  FRAMES_SEALED: Object.freeze({
    status: 409,
    retryable: false,
    message: '문구 생성에 사용한 대표 장면은 더 이상 변경할 수 없습니다.',
  }),
  THREADS_COPY_INCOMPLETE: Object.freeze({
    status: 409,
    retryable: false,
    message: '쓰레드 문구를 만들려면 대표 장면을 세 장 이상 저장해야 합니다.',
  }),
  THREADS_COPY_IN_PROGRESS: Object.freeze({
    status: 409,
    retryable: true,
    message: '이 작업의 쓰레드 문구를 이미 생성하고 있습니다.',
  }),
  THREADS_COPY_WRITE_FAILED: Object.freeze({
    status: 500,
    retryable: true,
    message: '쓰레드 문구 파일을 로컬 폴더에 저장하지 못했습니다.',
  }),
  THREADS_COPY_OUTPUT_INVALID: Object.freeze({
    status: 502,
    retryable: true,
    message: 'Codex가 올바른 쓰레드 문구 형식을 반환하지 않았습니다.',
  }),
  THREADS_COPY_INPUT_INVALID: Object.freeze({
    status: 422,
    retryable: false,
    message: '쓰레드 문구 분석 입력이 올바르지 않습니다.',
  }),
  CODEX_NOT_FOUND: Object.freeze({
    status: 503,
    retryable: false,
    message: 'Codex CLI를 찾을 수 없습니다.',
  }),
  CODEX_AUTH_REQUIRED: Object.freeze({
    status: 401,
    retryable: false,
    message: 'Codex CLI 로그인이 필요합니다.',
  }),
  CODEX_EXECUTION_FAILED: Object.freeze({
    status: 502,
    retryable: true,
    message: 'Codex가 쓰레드 문구 생성을 완료하지 못했습니다.',
  }),
  CODEX_TIMEOUT: Object.freeze({
    status: 504,
    retryable: true,
    message: 'Codex 문구 생성 시간이 초과되었습니다.',
  }),
  CODEX_OUTPUT_TOO_LARGE: Object.freeze({
    status: 502,
    retryable: true,
    message: 'Codex 출력이 허용된 크기를 초과했습니다.',
  }),
  CODEX_CLEANUP_FAILED: Object.freeze({
    status: 500,
    retryable: true,
    message: 'Codex 임시 분석 파일을 안전하게 정리하지 못했습니다.',
  }),
  CODEX_CONCURRENCY_LIMIT: Object.freeze({
    status: 429,
    retryable: true,
    message: '다른 쓰레드 문구를 생성하고 있습니다. 잠시 후 다시 시도해 주세요.',
  }),
  INTERNAL_ERROR: Object.freeze({
    status: 500,
    retryable: true,
    message: '요청을 처리하지 못했습니다.',
  }),
  UPSTREAM_SCHEMA_CHANGED: Object.freeze({
    status: 502,
    retryable: true,
    message: 'RedNote 응답 형식이 변경되었거나 접근이 제한됐습니다.',
  }),
  POST_NOT_FOUND: Object.freeze({
    status: 404,
    retryable: false,
    message: '공개 게시물을 찾을 수 없습니다.',
  }),
  NOT_A_VIDEO: Object.freeze({
    status: 422,
    retryable: false,
    message: '영상이 포함된 게시물이 아닙니다.',
  }),
  NO_COMPATIBLE_MEDIA: Object.freeze({
    status: 422,
    retryable: false,
    message: '호환 가능한 무워터마크 MP4 스트림을 찾지 못했습니다.',
  }),
  LOGIN_REQUIRED: Object.freeze({
    status: 403,
    retryable: false,
    message: '로그인 없이 공개적으로 접근할 수 없는 게시물입니다.',
  }),
  UPSTREAM_CHALLENGE: Object.freeze({
    status: 502,
    retryable: true,
    message: 'RedNote 접근 확인 화면으로 인해 게시물을 읽을 수 없습니다.',
  }),
  UPSTREAM_TIMEOUT: Object.freeze({
    status: 504,
    retryable: true,
    message: 'RedNote 응답 시간이 초과되었습니다.',
  }),
  REQUEST_CANCELLED: Object.freeze({
    status: 499,
    retryable: true,
    message: '요청이 취소되었습니다.',
  }),
  MEDIA_UNAVAILABLE: Object.freeze({
    status: 502,
    retryable: true,
    message: '사용 가능한 영상 다운로드 주소를 찾지 못했습니다.',
  }),
  MEDIA_DOWNLOAD_FAILED: Object.freeze({
    status: 502,
    retryable: true,
    message: '영상 다운로드에 실패했습니다.',
  }),
  MEDIA_TOO_LARGE: Object.freeze({
    status: 413,
    retryable: false,
    message: '영상 파일이 허용된 크기를 초과했습니다.',
  }),
  MEDIA_INVALID: Object.freeze({
    status: 502,
    retryable: true,
    message: '다운로드한 파일이 올바른 MP4 영상이 아닙니다.',
  }),
  MEDIA_TRUNCATED: Object.freeze({
    status: 502,
    retryable: true,
    message: '영상 다운로드가 완전히 끝나지 않았습니다.',
  }),
  OUTPUT_WRITE_FAILED: Object.freeze({
    status: 500,
    retryable: true,
    message: '영상 파일을 로컬 폴더에 저장하지 못했습니다.',
  }),
});

export class AppError extends Error {
  constructor(code) {
    const definition = ERROR_DEFINITIONS[code];

    if (!definition) {
      throw new TypeError('Unknown application error code.');
    }

    super(definition.message);
    this.name = 'AppError';
    this.code = code;
    this.status = definition.status;
    this.retryable = definition.retryable;
    this.publicMessage = definition.message;
  }

  toJSON() {
    return {
      code: this.code,
      status: this.status,
      retryable: this.retryable,
      message: this.publicMessage,
    };
  }
}
