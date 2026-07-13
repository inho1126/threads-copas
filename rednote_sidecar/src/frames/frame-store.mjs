import { randomBytes as cryptoRandomBytes } from 'node:crypto';
import { link, lstat, open, realpath, unlink } from 'node:fs/promises';
import path from 'node:path';

import { AppError } from '../errors.mjs';
import { isOutputAllocation } from '../files/output-paths.mjs';

export const MAX_JPEG_BYTES = 15 * 1024 * 1024;

const NOTE_ID = /^[a-f0-9]{24}$/u;
const TEMP_ATTEMPTS = 16;

function invalid() {
  return new AppError('FRAME_INVALID');
}

function writeFailed() {
  return new AppError('FRAME_WRITE_FAILED');
}

function formatTimestamp(timeMs) {
  const rounded = Math.round(timeMs);
  const minutes = Math.floor(rounded / 60_000);
  const seconds = Math.floor((rounded % 60_000) / 1_000);
  const milliseconds = rounded % 1_000;
  return `${String(minutes).padStart(2, '0')}m${String(seconds).padStart(2, '0')}s${String(milliseconds).padStart(3, '0')}`;
}

function identityOf(stats) {
  return Object.freeze({ dev: stats.dev, ino: stats.ino });
}

function hasIdentity(stats, identity) {
  return stats.dev === identity.dev && stats.ino === identity.ino;
}

async function verifyDirectory(targetPath, identity) {
  const stats = await lstat(targetPath);
  if (
    !stats.isDirectory()
    || stats.isSymbolicLink()
    || !hasIdentity(stats, identity)
    || (stats.mode & 0o077) !== 0
    || await realpath(targetPath) !== targetPath
  ) {
    throw writeFailed();
  }
}

async function verifyOutput(output, identity) {
  try {
    await verifyDirectory(output.rootPath, identity.root);
    await verifyDirectory(output.directoryPath, identity.directory);
  } catch (error) {
    if (error instanceof AppError) throw error;
    throw writeFailed();
  }
}

async function captureOutputIdentity(output, noteId) {
  if (!isOutputAllocation(output) || output.noteId !== noteId) throw invalid();
  const [rootPath, directoryPath, rootStats, directoryStats] = await Promise.all([
    realpath(output.rootPath),
    realpath(output.directoryPath),
    lstat(output.rootPath),
    lstat(output.directoryPath),
  ]).catch(() => { throw writeFailed(); });
  const relative = path.relative(rootPath, directoryPath);
  if (
    rootPath !== output.rootPath
    || directoryPath !== output.directoryPath
    || !rootStats.isDirectory()
    || rootStats.isSymbolicLink()
    || !directoryStats.isDirectory()
    || directoryStats.isSymbolicLink()
    || (rootStats.mode & 0o077) !== 0
    || (directoryStats.mode & 0o077) !== 0
    || relative === ''
    || relative.startsWith('..')
    || path.isAbsolute(relative)
  ) {
    throw writeFailed();
  }

  const identity = Object.freeze({
    root: identityOf(rootStats),
    directory: identityOf(directoryStats),
  });
  await verifyOutput(output, identity);
  return identity;
}

async function verifyOwnedFile(targetPath, identity) {
  const stats = await lstat(targetPath);
  if (
    !stats.isFile()
    || stats.isSymbolicLink()
    || !hasIdentity(stats, identity)
    || (stats.mode & 0o077) !== 0
  ) {
    throw writeFailed();
  }
  return stats;
}

async function unlinkOwnedFile(targetPath, identity) {
  await verifyOwnedFile(targetPath, identity);
  await unlink(targetPath);
}

async function cleanupOwnedFile(targetPath, identity) {
  if (!targetPath || !identity) return;
  try {
    await unlinkOwnedFile(targetPath, identity);
  } catch {
    // Never remove a path whose captured inode no longer matches our file.
  }
}

async function verifyTemp(output, outputIdentity, temp, { requireOpenHandle = true } = {}) {
  await verifyOutput(output, outputIdentity);
  await verifyOwnedFile(temp.tempPath, temp.identity);
  if (await realpath(temp.tempPath) !== temp.tempPath) throw writeFailed();
  if (requireOpenHandle) {
    const handleStats = await temp.handle.stat();
    if (!handleStats.isFile() || !hasIdentity(handleStats, temp.identity)) throw writeFailed();
  }
  await verifyOutput(output, outputIdentity);
}

async function writeAll(handle, bytes) {
  let offset = 0;
  while (offset < bytes.length) {
    const result = await handle.write(bytes, offset, bytes.length - offset, null);
    if (!Number.isInteger(result.bytesWritten) || result.bytesWritten < 1) throw writeFailed();
    offset += result.bytesWritten;
  }
}

export class FrameStore {
  #maxBytes;
  #randomBytes;
  #saved = new WeakMap();

  constructor({ maxBytes = MAX_JPEG_BYTES, randomBytes = cryptoRandomBytes } = {}) {
    if (
      !Number.isSafeInteger(maxBytes)
      || maxBytes < 5
      || maxBytes > MAX_JPEG_BYTES
      || typeof randomBytes !== 'function'
    ) {
      throw new TypeError('대표 장면 저장소 설정이 올바르지 않습니다.');
    }
    this.#maxBytes = maxBytes;
    this.#randomBytes = randomBytes;
  }

  async #openTemp(output, outputIdentity) {
    for (let attempt = 0; attempt < TEMP_ATTEMPTS; attempt += 1) {
      await verifyOutput(output, outputIdentity);
      const random = this.#randomBytes(16);
      if (!(random instanceof Uint8Array) || random.byteLength < 16) throw writeFailed();
      const tempPath = path.join(output.directoryPath, `.frame-${Buffer.from(random).toString('hex')}.part`);
      let actualPath;
      let handle;
      let identity;
      try {
        handle = await open(tempPath, 'wx+', 0o600);
        const stats = await handle.stat();
        identity = identityOf(stats);
        actualPath = await realpath(tempPath);
        const temp = { actualPath, handle, identity, tempPath };
        if (!stats.isFile() || (stats.mode & 0o077) !== 0 || actualPath !== tempPath) {
          throw writeFailed();
        }
        await verifyTemp(output, outputIdentity, temp);
        return temp;
      } catch (error) {
        await handle?.close().catch(() => {});
        await cleanupOwnedFile(actualPath ?? tempPath, identity);
        if (actualPath && actualPath !== tempPath) {
          await cleanupOwnedFile(tempPath, identity);
        }
        if (error?.code === 'EEXIST') continue;
        throw writeFailed();
      }
    }
    throw writeFailed();
  }

  async save(options = {}) {
    const {
      output,
      noteId,
      durationMs,
      index,
      timeMs,
      contentType,
      contentLength,
      body,
      signal,
    } = options;

    if (
      Object.hasOwn(options, 'fileName')
      || Object.hasOwn(options, 'filename')
      || Object.hasOwn(options, 'path')
      || !NOTE_ID.test(noteId)
      || !Number.isInteger(index)
      || index < 1
      || index > 5
      || !Number.isFinite(durationMs)
      || durationMs <= 0
      || !Number.isFinite(timeMs)
      || timeMs < 0
      || timeMs > durationMs
      || contentType !== 'image/jpeg'
      || (contentLength !== undefined && contentLength !== null && (
        !Number.isSafeInteger(contentLength) || contentLength < 0
      ))
      || body === null
      || typeof body?.[Symbol.asyncIterator] !== 'function'
      || (signal !== undefined && !(signal instanceof AbortSignal))
    ) {
      throw invalid();
    }
    if (contentLength > this.#maxBytes) throw new AppError('FRAME_TOO_LARGE');

    const outputIdentity = await captureOutputIdentity(output, noteId);
    let indices = this.#saved.get(output);
    if (!indices) {
      indices = new Set();
      this.#saved.set(output, indices);
    }
    if (indices.has(index)) throw new AppError('FRAME_DUPLICATE');
    indices.add(index);

    let finalFile;
    let temp;
    let published = false;

    try {
      temp = await this.#openTemp(output, outputIdentity);
      let size = 0;
      let head = Buffer.alloc(0);
      let tail = Buffer.alloc(0);

      for await (const chunk of body) {
        if (signal?.aborted) throw new AppError('REQUEST_CANCELLED');
        const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
        size += bytes.length;
        if (size > this.#maxBytes) throw new AppError('FRAME_TOO_LARGE');
        if (head.length < 3) head = Buffer.concat([head, bytes]).subarray(0, 3);
        tail = Buffer.concat([tail, bytes]).subarray(-2);
        await verifyTemp(output, outputIdentity, temp);
        await writeAll(temp.handle, bytes);
        await verifyTemp(output, outputIdentity, temp);
      }

      if (
        (contentLength !== undefined && contentLength !== null && size !== contentLength)
        || size < 5
        || head.length !== 3
        || head[0] !== 0xff
        || head[1] !== 0xd8
        || head[2] !== 0xff
        || tail[0] !== 0xff
        || tail[1] !== 0xd9
      ) {
        throw invalid();
      }

      await verifyTemp(output, outputIdentity, temp);
      await temp.handle.sync();
      await verifyTemp(output, outputIdentity, temp);
      await temp.handle.close();
      temp.handle = undefined;
      await verifyTemp(output, outputIdentity, temp, { requireOpenHandle: false });

      const base = `rednote-${noteId}-${formatTimestamp(timeMs)}`;
      let fileName;
      for (let suffix = 1; ; suffix += 1) {
        fileName = `${base}${suffix === 1 ? '' : `-${suffix}`}.jpg`;
        const finalPath = path.join(output.directoryPath, fileName);
        try {
          await verifyTemp(output, outputIdentity, temp, { requireOpenHandle: false });
          await link(temp.tempPath, finalPath);
          finalFile = { actualPath: undefined, identity: temp.identity, path: finalPath };
          const finalStats = await verifyOwnedFile(finalPath, temp.identity);
          const actualPath = await realpath(finalPath);
          finalFile = { actualPath, identity: identityOf(finalStats), path: finalPath };
          await verifyOutput(output, outputIdentity);
          if (actualPath !== finalPath || !hasIdentity(finalStats, temp.identity)) throw writeFailed();
          await verifyTemp(output, outputIdentity, temp, { requireOpenHandle: false });
          break;
        } catch (error) {
          if (error?.code !== 'EEXIST') throw writeFailed();
        }
      }

      await unlinkOwnedFile(temp.tempPath, temp.identity);
      temp = undefined;
      published = true;
      return Object.freeze({ index, timeMs, fileName });
    } catch (error) {
      if (error instanceof AppError) throw error;
      if (signal?.aborted) throw new AppError('REQUEST_CANCELLED');
      throw writeFailed();
    } finally {
      await temp?.handle?.close().catch(() => {});
      if (!published) {
        await cleanupOwnedFile(finalFile?.actualPath ?? finalFile?.path, finalFile?.identity);
        if (finalFile?.actualPath && finalFile.actualPath !== finalFile.path) {
          await cleanupOwnedFile(finalFile.path, finalFile.identity);
        }
        await cleanupOwnedFile(temp?.actualPath ?? temp?.tempPath, temp?.identity);
        if (temp?.actualPath && temp.actualPath !== temp.tempPath) {
          await cleanupOwnedFile(temp.tempPath, temp.identity);
        }
      }
      if (!published) indices.delete(index);
    }
  }

  saveFrame(options) {
    return this.save(options);
  }
}
