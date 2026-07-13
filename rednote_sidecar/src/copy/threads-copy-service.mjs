import path from 'node:path';

import { AppError } from '../errors.mjs';
import { isOutputAllocation, publishOutputArtifacts } from '../files/output-paths.mjs';
import { runCodexThreadsCopy } from './codex-runner.mjs';
import {
  THREAD_STYLE_DEFINITIONS,
  validateThreadsCopyOutput,
} from './threads-copy.mjs';

let activeCodexRuns = 0;
const MAX_CODEX_OUTPUT_ATTEMPTS = 2;

function inputError() {
  return new AppError('THREADS_COPY_INPUT_INVALID');
}

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function fileNames(generation) {
  const suffix = generation === 1 ? '' : `-${generation}`;
  return Object.freeze({
    text: `threads-copy${suffix}.txt`,
    json: `threads-copy${suffix}.json`,
  });
}

function copyStyles(raw) {
  return Object.freeze(THREAD_STYLE_DEFINITIONS.map(({ key, label }) => Object.freeze({
    style: key,
    label,
    hooks: raw[key].hooks,
    body: raw[key].body,
  })));
}

function buildResult(generation, generatedAt, styles) {
  return Object.freeze({
    generation,
    generatedAt,
    styles,
    files: fileNames(generation),
  });
}

async function generateValidatedCopy(runner, input) {
  for (let attempt = 1; attempt <= MAX_CODEX_OUTPUT_ATTEMPTS; attempt += 1) {
    try {
      const runnerInput = attempt === 1
        ? input
        : Object.freeze({ ...input, validationRetry: true });
      return validateThreadsCopyOutput(await runner(runnerInput));
    } catch (error) {
      const canRetry = error instanceof AppError
        && error.code === 'THREADS_COPY_OUTPUT_INVALID'
        && attempt < MAX_CODEX_OUTPUT_ATTEMPTS;
      if (!canRetry) throw error;
    }
  }

  throw new AppError('THREADS_COPY_OUTPUT_INVALID');
}

export function formatThreadsCopyText(result) {
  if (!isRecord(result) || !Array.isArray(result.styles)) throw inputError();
  const lines = [
    'Threads 문구 생성 결과',
    `생성 시각: ${result.generatedAt}`,
    `세대: ${result.generation}`,
    '',
  ];

  for (const style of result.styles) {
    lines.push(
      `[${style.label}]`,
      '첫 문장 후보',
      ...style.hooks.map((hook, index) => `${index + 1}. ${hook}`),
      '',
      '완성형 본문',
      style.body,
      '',
    );
  }

  return `${lines.join('\n').trimEnd()}\n`;
}

function validateJob(job, generation) {
  if (
    !isRecord(job)
    || !isOutputAllocation(job.output)
    || !isRecord(job.analysisContext)
    || !Array.isArray(job.frames)
    || job.frames.length < 3
    || job.frames.length > 5
    || !Number.isSafeInteger(generation)
    || generation < 1
  ) {
    throw inputError();
  }

  const { canonicalUrl } = job.analysisContext;

  try {
    const url = new URL(canonicalUrl);
    if (
      url.protocol !== 'https:'
      || url.hostname !== 'www.rednote.com'
      || url.search !== ''
      || url.hash !== ''
      || url.username !== ''
      || url.password !== ''
      || url.port !== ''
      || url.pathname !== `/explore/${job.note.noteId}`
      || url.href !== canonicalUrl
    ) {
      throw inputError();
    }
  } catch (error) {
    if (error instanceof AppError) throw error;
    throw inputError();
  }

  const imagePaths = job.frames.map((frame) => {
    if (
      !isRecord(frame)
      || typeof frame.fileName !== 'string'
      || path.basename(frame.fileName) !== frame.fileName
      || !/^rednote-[a-f0-9]{24}-.+\.jpg$/u.test(frame.fileName)
    ) {
      throw inputError();
    }

    const imagePath = path.join(job.output.directoryPath, frame.fileName);

    if (path.dirname(imagePath) !== job.output.directoryPath) throw inputError();
    return imagePath;
  });

  return Object.freeze(imagePaths);
}

export class ThreadsCopyService {
  #now;
  #publisher;
  #runner;

  constructor({
    now = Date.now,
    publisher = publishOutputArtifacts,
    runner = runCodexThreadsCopy,
  } = {}) {
    if (
      typeof now !== 'function'
      || typeof publisher !== 'function'
      || typeof runner !== 'function'
    ) {
      throw new TypeError('쓰레드 문구 서비스 설정이 올바르지 않습니다.');
    }

    this.#now = now;
    this.#publisher = publisher;
    this.#runner = runner;
  }

  async generate({ job, generation, signal } = {}) {
    const imagePaths = validateJob(job, generation);

    if (activeCodexRuns >= 1) throw new AppError('CODEX_CONCURRENCY_LIMIT');
    activeCodexRuns += 1;

    let raw;

    try {
      raw = await generateValidatedCopy(this.#runner, {
        analysisContext: job.analysisContext,
        imagePaths,
        signal,
      });
    } finally {
      activeCodexRuns -= 1;
    }

    const now = this.#now();

    if (!Number.isSafeInteger(now) || now < 0) {
      throw new TypeError('생성 시각이 올바르지 않습니다.');
    }

    const generatedAt = new Date(now).toISOString();
    const styles = copyStyles(raw);
    const publication = await this.#publisher(
      job.output,
      generation,
      (actualGeneration) => {
        const result = buildResult(actualGeneration, generatedAt, styles);
        return {
          text: formatThreadsCopyText(result),
          json: `${JSON.stringify(result, null, 2)}\n`,
        };
      },
    );

    if (
      !isRecord(publication)
      || !Number.isSafeInteger(publication.generation)
      || publication.generation < generation
      || publication.textFileName !== fileNames(publication.generation).text
      || publication.jsonFileName !== fileNames(publication.generation).json
    ) {
      throw new AppError('THREADS_COPY_WRITE_FAILED');
    }

    return buildResult(publication.generation, generatedAt, styles);
  }
}
