import { requireOpaqueId } from './video-frames.js';
import { containsForbiddenThreadsReference } from './threads-copy-safety.js';

const MAX_HOOK_CHARACTERS = 25;
const MAX_BODY_CHARACTERS = 140;
const CONTROL_CHARACTER = /\p{Cc}/u;
const HANGUL_CHARACTER = /\p{Script=Hangul}/u;
const HAN_CHARACTER = /\p{Script=Han}/u;

const STYLE_DEFINITIONS = Object.freeze([
  Object.freeze({ style: 'curiosity', label: '호기심 폭발형' }),
  Object.freeze({ style: 'discovery', label: '솔직한 발견·후기형' }),
  Object.freeze({ style: 'conversion', label: '구매 전환형' }),
]);

function invalidCopyResult() {
  return new TypeError('문구 생성 결과가 올바르지 않습니다.');
}

function isPlainRecord(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function hasExactKeys(value, expected) {
  if (!isPlainRecord(value) || Object.getOwnPropertySymbols(value).length !== 0) return false;
  const actual = Object.keys(value).sort();
  const sortedExpected = [...expected].sort();
  return actual.length === sortedExpected.length
    && actual.every((key, index) => key === sortedExpected[index]);
}

function hasUnsafeCharacter(value, { allowLayout }) {
  for (const character of value) {
    if (allowLayout && (character === '\t' || character === '\n')) continue;
    const codePoint = character.codePointAt(0);
    if (
      CONTROL_CHARACTER.test(character)
      || (codePoint >= 0xd800 && codePoint <= 0xdfff)
      || codePoint === 0x061c
      || codePoint === 0x200e
      || codePoint === 0x200f
      || (codePoint >= 0x202a && codePoint <= 0x202e)
      || (codePoint >= 0x2066 && codePoint <= 0x2069)
    ) {
      return true;
    }
  }
  return false;
}

function validText(value, maximumCharacters, options) {
  return typeof value === 'string'
    && value.length > 0
    && value.trim() === value
    && [...value].length <= maximumCharacters
    && HANGUL_CHARACTER.test(value)
    && !HAN_CHARACTER.test(value)
    && !containsForbiddenThreadsReference(value)
    && !hasUnsafeCharacter(value, options);
}

function fileNames(generation) {
  const suffix = generation === 1 ? '' : `-${generation}`;
  return Object.freeze({
    text: `threads-copy${suffix}.txt`,
    json: `threads-copy${suffix}.json`,
  });
}

export function buildThreadsCopyUrl(jobId) {
  return `/api/jobs/${requireOpaqueId(jobId, 'jobId')}/threads-copy`;
}

export function validateThreadsCopyResponse(value) {
  if (
    !hasExactKeys(value, ['ok', 'generation', 'generatedAt', 'styles', 'files'])
    || value.ok !== true
    || !Number.isSafeInteger(value.generation)
    || value.generation < 1
    || typeof value.generatedAt !== 'string'
    || !Array.isArray(value.styles)
    || value.styles.length !== STYLE_DEFINITIONS.length
    || !hasExactKeys(value.files, ['text', 'json'])
  ) {
    throw invalidCopyResult();
  }

  try {
    if (new Date(value.generatedAt).toISOString() !== value.generatedAt) throw invalidCopyResult();
  } catch {
    throw invalidCopyResult();
  }

  const expectedFiles = fileNames(value.generation);
  if (value.files.text !== expectedFiles.text || value.files.json !== expectedFiles.json) {
    throw invalidCopyResult();
  }

  const styles = value.styles.map((source, index) => {
    const expected = STYLE_DEFINITIONS[index];
    if (
      !hasExactKeys(source, ['style', 'label', 'hooks', 'body'])
      || source.style !== expected.style
      || source.label !== expected.label
      || !Array.isArray(source.hooks)
      || source.hooks.length !== 3
      || !validText(source.body, MAX_BODY_CHARACTERS, { allowLayout: true })
    ) {
      throw invalidCopyResult();
    }

    const hooks = source.hooks.map((hook) => {
      if (!validText(hook, MAX_HOOK_CHARACTERS, { allowLayout: false })) {
        throw invalidCopyResult();
      }
      return hook;
    });
    if (new Set(hooks).size !== hooks.length) throw invalidCopyResult();

    return Object.freeze({
      style: expected.style,
      label: expected.label,
      hooks: Object.freeze(hooks),
      body: source.body,
    });
  });

  return Object.freeze({
    generation: value.generation,
    generatedAt: value.generatedAt,
    styles: Object.freeze(styles),
    files: expectedFiles,
  });
}

export function formatThreadCardCopy(style) {
  if (
    !hasExactKeys(style, ['style', 'label', 'hooks', 'body'])
    || !STYLE_DEFINITIONS.some(
      (definition) => definition.style === style.style && definition.label === style.label,
    )
    || !Array.isArray(style.hooks)
    || style.hooks.length !== 3
    || style.hooks.some((hook) => !validText(hook, MAX_HOOK_CHARACTERS, { allowLayout: false }))
    || new Set(style.hooks).size !== style.hooks.length
    || !validText(style.body, MAX_BODY_CHARACTERS, { allowLayout: true })
  ) {
    throw invalidCopyResult();
  }

  return [
    `[${style.label}]`,
    '첫 문장 후보',
    ...style.hooks.map((hook, index) => `${index + 1}. ${hook}`),
    '',
    '완성형 본문',
    style.body,
  ].join('\n');
}
