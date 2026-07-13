import { randomBytes as cryptoRandomBytes } from 'node:crypto';
import {
  chmod,
  link,
  lstat,
  mkdir,
  open,
  realpath,
  rmdir,
  unlink,
} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { Writable } from 'node:stream';

import { AppError } from '../errors.mjs';

const NOTE_ID = /^[0-9a-f]{24}$/u;
const DIRECTORY_MODE = 0o700;
const PART_MODE = 0o600;
const MAX_ARTIFACT_BYTES = 1024 * 1024;
const ARTIFACT_RANDOM_BYTES = 16;
const MAX_ARTIFACT_TEMP_ATTEMPTS = 16;
const OUTPUT_ALLOCATIONS = new WeakMap();

function outputError() {
  return new AppError('OUTPUT_WRITE_FAILED');
}

function artifactError() {
  return new AppError('THREADS_COPY_WRITE_FAILED');
}

function artifactDataError() {
  return new TypeError('쓰레드 문구 파일 데이터가 올바르지 않습니다.');
}

function isPlainRecord(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return false;
  }

  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function hasExactKeys(value, keys) {
  if (!isPlainRecord(value)) return false;
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length
    && actual.every((key, index) => key === expected[index]);
}

function isContained(rootPath, targetPath) {
  const relative = path.relative(rootPath, targetPath);
  return relative !== '..'
    && !relative.startsWith(`..${path.sep}`)
    && !path.isAbsolute(relative);
}

function identityOf(stats) {
  return Object.freeze({ dev: stats.dev, ino: stats.ino });
}

function hasIdentity(stats, identity) {
  return stats.dev === identity.dev && stats.ino === identity.ino;
}

function privateRecord(output) {
  const record = OUTPUT_ALLOCATIONS.get(output);

  if (!record) {
    throw new TypeError('유효한 출력 경로 할당이 아닙니다.');
  }

  return record;
}

async function verifyDirectory(targetPath, canonicalPath, identity) {
  const stats = await lstat(targetPath);

  if (
    !stats.isDirectory()
    || stats.isSymbolicLink()
    || !hasIdentity(stats, identity)
    || (stats.mode & 0o077) !== 0
    || await realpath(targetPath) !== canonicalPath
  ) {
    throw outputError();
  }
}

async function verifyCanonicalExistingAncestor(targetPath) {
  let candidate = targetPath;

  while (true) {
    try {
      const stats = await lstat(candidate);

      if (
        !stats.isDirectory()
        || stats.isSymbolicLink()
        || await realpath(candidate) !== candidate
      ) {
        throw outputError();
      }

      return;
    } catch (error) {
      if (error instanceof AppError) throw error;
      if (error?.code !== 'ENOENT') throw outputError();

      const parent = path.dirname(candidate);

      if (parent === candidate) {
        throw outputError();
      }

      candidate = parent;
    }
  }
}

async function verifyDirectories(output, record) {
  try {
    await verifyDirectory(
      output.rootPath,
      output.rootPath,
      record.identity.root,
    );
    await verifyDirectory(
      output.directoryPath,
      output.directoryPath,
      record.identity.directory,
    );
  } catch (error) {
    if (error instanceof AppError) throw error;
    throw outputError();
  }
}

async function verifyPart(output, record, { requireOpenHandle = true } = {}) {
  await verifyDirectories(output, record);

  if (!record.part) {
    throw outputError();
  }

  try {
    const pathStats = await lstat(output.partPath);

    if (
      !pathStats.isFile()
      || pathStats.isSymbolicLink()
      || !hasIdentity(pathStats, record.part.identity)
      || (pathStats.mode & 0o077) !== 0
      || await realpath(output.partPath) !== output.partPath
    ) {
      throw outputError();
    }

    if (requireOpenHandle) {
      const handleStats = await record.part.handle.stat();

      if (!handleStats.isFile() || !hasIdentity(handleStats, record.part.identity)) {
        throw outputError();
      }
    }
  } catch (error) {
    if (error instanceof AppError) throw error;
    throw outputError();
  }
}

async function closePart(record) {
  if (!record.part || record.part.closed) return;
  record.part.closed = true;
  await record.part.handle.close().catch(() => {});
}

function artifactNames(generation) {
  const suffix = generation === 1 ? '' : `-${generation}`;
  return Object.freeze({
    textFileName: `threads-copy${suffix}.txt`,
    jsonFileName: `threads-copy${suffix}.json`,
  });
}

function validArtifactContent(value) {
  return typeof value === 'string'
    && Buffer.byteLength(value) > 0
    && Buffer.byteLength(value) <= MAX_ARTIFACT_BYTES;
}

async function verifyOwnedArtifact(output, record, filePath, identity, handle) {
  await verifyDirectories(output, record);

  try {
    const stats = await lstat(filePath);

    if (
      !stats.isFile()
      || stats.isSymbolicLink()
      || !hasIdentity(stats, identity)
      || (stats.mode & 0o077) !== 0
      || await realpath(filePath) !== filePath
    ) {
      throw artifactError();
    }

    if (handle) {
      const handleStats = await handle.stat();

      if (!handleStats.isFile() || !hasIdentity(handleStats, identity)) {
        throw artifactError();
      }
    }
  } catch (error) {
    if (error instanceof AppError) throw error;
    throw artifactError();
  }
}

async function closeArtifact(artifact) {
  if (!artifact || artifact.closed) return;
  artifact.closed = true;
  await artifact.handle.close().catch(() => {});
}

async function unlinkOwnedArtifact(output, record, filePath, identity) {
  try {
    await verifyDirectories(output, record);
    const stats = await lstat(filePath);

    if (!stats.isFile() || stats.isSymbolicLink() || !hasIdentity(stats, identity)) {
      return true;
    }

    await unlink(filePath);
    await verifyDirectories(output, record);
    return true;
  } catch (error) {
    if (error?.code === 'ENOENT') return true;
    return false;
  }
}

async function createArtifactTemp(
  output,
  record,
  kind,
  content,
  randomBytes,
) {
  for (let attempt = 0; attempt < MAX_ARTIFACT_TEMP_ATTEMPTS; attempt += 1) {
    await verifyDirectories(output, record);
    const bytes = randomBytes(ARTIFACT_RANDOM_BYTES);

    if (!(bytes instanceof Uint8Array) || bytes.byteLength < ARTIFACT_RANDOM_BYTES) {
      throw artifactError();
    }

    const tempPath = path.join(
      output.directoryPath,
      `.threads-copy-${kind}-${Buffer.from(bytes).toString('hex')}.part`,
    );
    await verifyDirectories(output, record);
    let handle;

    try {
      handle = await open(tempPath, 'wx+', PART_MODE);
    } catch (error) {
      if (error?.code === 'EEXIST') continue;
      throw artifactError();
    }

    let artifact;

    try {
      const stats = await handle.stat();

      if (!stats.isFile() || (stats.mode & 0o077) !== 0) {
        throw artifactError();
      }

      artifact = {
        closed: false,
        handle,
        identity: identityOf(stats),
        path: tempPath,
      };
      await verifyOwnedArtifact(output, record, tempPath, artifact.identity, handle);
      await handle.writeFile(content, 'utf8');
      await handle.sync();
      await verifyOwnedArtifact(output, record, tempPath, artifact.identity, handle);
      return artifact;
    } catch (error) {
      await closeArtifact(artifact ?? { closed: false, handle });

      if (artifact) {
        await unlinkOwnedArtifact(output, record, tempPath, artifact.identity);
      }

      if (error instanceof AppError) throw error;
      throw artifactError();
    }
  }

  throw artifactError();
}

async function cleanupArtifactAttempt(output, record, artifacts, finals) {
  for (const artifact of artifacts) {
    await closeArtifact(artifact);
  }

  let safe = true;

  for (const final of finals) {
    safe = await unlinkOwnedArtifact(output, record, final.path, final.identity) && safe;
  }

  for (const artifact of artifacts) {
    safe = await unlinkOwnedArtifact(output, record, artifact.path, artifact.identity) && safe;
  }

  return safe;
}

export function getDefaultOutputRoot() {
  return path.join(os.homedir(), 'Downloads', 'rednote');
}

export function isOutputAllocation(value) {
  return value !== null
    && typeof value === 'object'
    && OUTPUT_ALLOCATIONS.has(value);
}

export async function allocateOutputPaths(
  noteId,
  { root = getDefaultOutputRoot() } = {},
) {
  if (!NOTE_ID.test(noteId) || typeof root !== 'string' || !path.isAbsolute(root)) {
    throw new TypeError('출력 경로 설정이 올바르지 않습니다.');
  }

  const requestedRoot = path.resolve(root);
  let rootPath;
  let rootStats;

  try {
    await verifyCanonicalExistingAncestor(requestedRoot);
    await mkdir(requestedRoot, { recursive: true, mode: DIRECTORY_MODE });
    rootStats = await lstat(requestedRoot);
    rootPath = await realpath(requestedRoot);

    if (
      rootPath !== requestedRoot
      || !rootStats.isDirectory()
      || rootStats.isSymbolicLink()
    ) {
      throw outputError();
    }

    await chmod(rootPath, DIRECTORY_MODE);
    rootStats = await lstat(rootPath);

    if (
      rootPath !== requestedRoot
      || !rootStats.isDirectory()
      || rootStats.isSymbolicLink()
    ) {
      throw outputError();
    }
  } catch (error) {
    if (error instanceof AppError) throw error;
    throw outputError();
  }

  const rootIdentity = identityOf(rootStats);

  for (let suffix = 1; Number.isSafeInteger(suffix); suffix += 1) {
    const directoryName = suffix === 1 ? noteId : `${noteId}-${suffix}`;
    const directoryPath = path.join(rootPath, directoryName);

    if (!isContained(rootPath, directoryPath)) {
      throw outputError();
    }

    try {
      await verifyDirectory(rootPath, rootPath, rootIdentity);
      await mkdir(directoryPath, { mode: DIRECTORY_MODE });
      await chmod(directoryPath, DIRECTORY_MODE);
      await verifyDirectory(rootPath, rootPath, rootIdentity);
      const canonicalDirectory = await realpath(directoryPath);
      const directoryStats = await lstat(directoryPath);

      if (
        canonicalDirectory !== directoryPath
        || !isContained(rootPath, canonicalDirectory)
        || !directoryStats.isDirectory()
        || directoryStats.isSymbolicLink()
      ) {
        throw outputError();
      }

      const filename = `rednote-${noteId}.mp4`;
      const videoPath = path.join(canonicalDirectory, filename);
      const allocation = Object.freeze({
        noteId,
        rootPath,
        directoryPath: canonicalDirectory,
        videoPath,
        partPath: `${videoPath}.part`,
      });
      OUTPUT_ALLOCATIONS.set(allocation, {
        identity: Object.freeze({
          root: rootIdentity,
          directory: identityOf(directoryStats),
        }),
        part: null,
        state: 'allocated',
      });
      return allocation;
    } catch (error) {
      if (error?.code === 'EEXIST') {
        continue;
      }

      if (error instanceof AppError) throw error;
      throw outputError();
    }
  }

  throw outputError();
}

export async function ensureOutputDestinationAbsent(output) {
  const record = privateRecord(output);

  if (record.state !== 'allocated') throw outputError();
  await verifyDirectories(output, record);

  try {
    await lstat(output.videoPath);
    throw outputError();
  } catch (error) {
    if (error instanceof AppError) throw error;
    if (error?.code !== 'ENOENT') throw outputError();
  }

  await verifyDirectories(output, record);
}

export async function createOutputPartWriteStream(output) {
  const record = privateRecord(output);

  if (record.state !== 'allocated') throw outputError();

  if (!record.part) {
    await verifyDirectories(output, record);
    let handle;

    try {
      handle = await open(output.partPath, 'wx+', PART_MODE);
      const stats = await handle.stat();

      if (!stats.isFile() || (stats.mode & 0o077) !== 0) {
        throw outputError();
      }

      record.part = {
        closed: false,
        handle,
        identity: identityOf(stats),
      };
      await verifyPart(output, record);
    } catch (error) {
      if (!record.part) await handle?.close().catch(() => {});
      if (error instanceof AppError) throw error;
      throw outputError();
    }
  }

  await verifyPart(output, record);

  try {
    await record.part.handle.truncate(0);
    await verifyPart(output, record);
    let position = 0;
    return new Writable({
      write(chunk, encoding, callback) {
        const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk, encoding);

        (async () => {
          let offset = 0;

          while (offset < buffer.length) {
            const { bytesWritten } = await record.part.handle.write(
              buffer,
              offset,
              buffer.length - offset,
              position + offset,
            );

            if (bytesWritten < 1) throw outputError();
            offset += bytesWritten;
          }

          position += buffer.length;
        })().then(() => callback(), callback);
      },
    });
  } catch (error) {
    if (error instanceof AppError) throw error;
    throw outputError();
  }
}

export async function inspectOutputPart(output, maxHeaderBytes = 64) {
  const record = privateRecord(output);

  if (!Number.isSafeInteger(maxHeaderBytes) || maxHeaderBytes < 1) {
    throw new TypeError('검사할 헤더 크기가 올바르지 않습니다.');
  }

  await verifyPart(output, record);

  try {
    const stats = await record.part.handle.stat();
    const head = Buffer.alloc(Math.min(maxHeaderBytes, stats.size));
    const { bytesRead } = await record.part.handle.read(head, 0, head.length, 0);
    await verifyPart(output, record);
    return Object.freeze({
      fileSize: stats.size,
      head: head.subarray(0, bytesRead),
    });
  } catch (error) {
    if (error instanceof AppError) throw error;
    throw outputError();
  }
}

export async function publishOutputPart(output) {
  const record = privateRecord(output);

  if (record.state !== 'allocated') throw outputError();
  await verifyPart(output, record);

  try {
    await record.part.handle.sync();
    await verifyPart(output, record);
    await link(output.partPath, output.videoPath);
    const finalStats = await lstat(output.videoPath);
    await verifyPart(output, record);

    if (!finalStats.isFile() || !hasIdentity(finalStats, record.part.identity)) {
      throw outputError();
    }

    await verifyPart(output, record);
    await unlink(output.partPath);
    record.state = 'published';
    await closePart(record);
  } catch (error) {
    if (error instanceof AppError) throw error;
    throw outputError();
  }
}

export async function publishOutputArtifacts(
  output,
  generation,
  artifacts,
  {
    randomBytes = cryptoRandomBytes,
    linkFile = link,
  } = {},
) {
  if (
    !Number.isSafeInteger(generation)
    || generation < 1
    || (typeof artifacts !== 'function' && !hasExactKeys(artifacts, ['text', 'json']))
    || typeof randomBytes !== 'function'
    || typeof linkFile !== 'function'
  ) {
    throw artifactDataError();
  }

  if (
    typeof artifacts !== 'function'
    && (!validArtifactContent(artifacts.text) || !validArtifactContent(artifacts.json))
  ) {
    throw artifactDataError();
  }

  const record = privateRecord(output);

  if (record.state === 'closed') throw artifactError();

  let candidate = generation;

  while (Number.isSafeInteger(candidate)) {
    let textTemp;
    let jsonTemp;
    let candidateArtifacts;

    try {
      candidateArtifacts = typeof artifacts === 'function'
        ? artifacts(candidate)
        : artifacts;
    } catch {
      throw artifactDataError();
    }

    if (
      !hasExactKeys(candidateArtifacts, ['text', 'json'])
      || !validArtifactContent(candidateArtifacts.text)
      || !validArtifactContent(candidateArtifacts.json)
    ) {
      throw artifactDataError();
    }

    try {
      textTemp = await createArtifactTemp(
        output,
        record,
        'txt',
        candidateArtifacts.text,
        randomBytes,
      );
      jsonTemp = await createArtifactTemp(
        output,
        record,
        'json',
        candidateArtifacts.json,
        randomBytes,
      );
    } catch {
      if (textTemp) {
        await cleanupArtifactAttempt(output, record, [textTemp], []);
      }
      throw artifactError();
    }

    const names = artifactNames(candidate);
    const textPath = path.join(output.directoryPath, names.textFileName);
    const jsonPath = path.join(output.directoryPath, names.jsonFileName);
    const ownedFinals = [
      { path: textPath, identity: textTemp.identity },
      { path: jsonPath, identity: jsonTemp.identity },
    ];

    try {
      await verifyDirectories(output, record);
      await linkFile(textTemp.path, textPath);
      await verifyOwnedArtifact(output, record, textPath, textTemp.identity);
      await verifyDirectories(output, record);
      await linkFile(jsonTemp.path, jsonPath);
      await verifyOwnedArtifact(output, record, jsonPath, jsonTemp.identity);
    } catch (error) {
      const collision = error?.code === 'EEXIST';
      const cleaned = await cleanupArtifactAttempt(
        output,
        record,
        [textTemp, jsonTemp],
        ownedFinals,
      );

      if (!cleaned || !collision || candidate === Number.MAX_SAFE_INTEGER) {
        throw artifactError();
      }

      candidate += 1;
      continue;
    }

    await closeArtifact(textTemp);
    await closeArtifact(jsonTemp);
    const textTempRemoved = await unlinkOwnedArtifact(
      output,
      record,
      textTemp.path,
      textTemp.identity,
    );
    const jsonTempRemoved = await unlinkOwnedArtifact(
      output,
      record,
      jsonTemp.path,
      jsonTemp.identity,
    );

    if (!textTempRemoved || !jsonTempRemoved) {
      await cleanupArtifactAttempt(
        output,
        record,
        [textTemp, jsonTemp],
        ownedFinals,
      );
      throw artifactError();
    }

    return Object.freeze({
      generation: candidate,
      ...names,
      textPath,
      jsonPath,
    });
  }

  throw artifactError();
}

export async function cleanupOutputAllocation(output) {
  const record = privateRecord(output);

  if (record.state === 'published' || record.state === 'closed') return;

  let partPathIsSafe = false;

  if (record.part) {
    try {
      await verifyPart(output, record);
      partPathIsSafe = true;
    } catch {
      partPathIsSafe = false;
    }

    await closePart(record);

    if (partPathIsSafe) {
      try {
        await verifyPart(output, record, { requireOpenHandle: false });
        await unlink(output.partPath);
      } catch {
        // Never delete through a path whose captured identity no longer matches.
      }
    }
  }

  try {
    await verifyDirectories(output, record);
    await rmdir(output.directoryPath);
  } catch {
    // Non-empty, moved, or identity-mismatched directories are intentionally preserved.
  }

  record.state = 'closed';
}
