import { spawn } from 'node:child_process';
import { constants as fsConstants } from 'node:fs';
import {
  chmod,
  copyFile,
  lstat,
  mkdtemp,
  open,
  rm,
  writeFile,
} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

import { AppError } from '../errors.mjs';
import {
  THREADS_COPY_SCHEMA,
  buildThreadsCopyPrompt,
  validateThreadsCopyOutput,
} from './threads-copy.mjs';

export const DEFAULT_CODEX_TIMEOUT_MS = 180_000;
export const DEFAULT_CODEX_KILL_GRACE_MS = 2_000;
export const DEFAULT_CODEX_PROCESS_OUTPUT_BYTES = 256 * 1024;
export const DEFAULT_CODEX_RESULT_BYTES = 256 * 1024;

const MAX_IMAGE_BYTES = 15 * 1024 * 1024;
const SCRATCH_MODE = 0o700;
const PRIVATE_FILE_MODE = 0o600;
const SCRATCH_CLEANUP_ATTEMPTS = 3;
const DISABLED_FEATURES = Object.freeze([
  'hooks',
  'shell_tool',
  'unified_exec',
  'shell_snapshot',
  'guardian_approval',
  'apps',
  'plugins',
  'plugin_sharing',
  'remote_plugin',
  'tool_call_mcp_elicitation',
  'skill_mcp_dependency_install',
  'tool_suggest',
  'browser_use',
  'browser_use_external',
  'browser_use_full_cdp_access',
  'in_app_browser',
  'computer_use',
  'image_generation',
  'multi_agent',
  'goals',
  'workspace_dependencies',
]);

function inputError() {
  return new AppError('THREADS_COPY_INPUT_INVALID');
}

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function identityMatches(first, second) {
  return first.dev === second.dev && first.ino === second.ino;
}

function sanitizeExecutablePath(value) {
  return value
    .split(path.delimiter)
    .filter((entry) => {
      if (entry.length === 0) return false;
      const normalized = path.normalize(entry);
      return !(
        path.basename(normalized).toLowerCase() === '.bin'
        && path.basename(path.dirname(normalized)).toLowerCase() === 'node_modules'
      );
    })
    .join(path.delimiter);
}

function validateRunnerInput({ analysisContext, imagePaths, signal, validationRetry } = {}) {
  if (
    !isRecord(analysisContext)
    || !Array.isArray(imagePaths)
    || imagePaths.length < 3
    || imagePaths.length > 5
    || imagePaths.some((value) => (
      typeof value !== 'string'
      || !path.isAbsolute(value)
      || path.extname(value).toLowerCase() !== '.jpg'
    ))
    || (validationRetry !== undefined && typeof validationRetry !== 'boolean')
    || (signal !== undefined
      && (typeof signal?.aborted !== 'boolean'
        || typeof signal.addEventListener !== 'function'
        || typeof signal.removeEventListener !== 'function'))
  ) {
    throw inputError();
  }
}

function validateOptions(options) {
  if (
    typeof options.spawnProcess !== 'function'
    || typeof options.signalProcess !== 'function'
    || typeof options.removeDirectory !== 'function'
    || typeof options.codexCommand !== 'string'
    || options.codexCommand.length === 0
    || typeof options.tempRoot !== 'string'
    || !path.isAbsolute(options.tempRoot)
    || !isRecord(options.sourceEnv)
    || !Number.isSafeInteger(options.timeoutMs)
    || options.timeoutMs < 1
    || !Number.isSafeInteger(options.killGraceMs)
    || options.killGraceMs < 1
    || !Number.isSafeInteger(options.maxProcessOutputBytes)
    || options.maxProcessOutputBytes < 1
    || !Number.isSafeInteger(options.maxResultBytes)
    || options.maxResultBytes < 1
  ) {
    throw new TypeError('Codex 실행기 설정이 올바르지 않습니다.');
  }
}

async function copyImageToScratch(sourcePath, destinationPath) {
  let before;

  try {
    before = await lstat(sourcePath);

    if (
      !before.isFile()
      || before.isSymbolicLink()
      || before.size < 4
      || before.size > MAX_IMAGE_BYTES
    ) {
      throw inputError();
    }

    await copyFile(sourcePath, destinationPath, fsConstants.COPYFILE_EXCL);
    await chmod(destinationPath, PRIVATE_FILE_MODE);
    const [after, copied] = await Promise.all([
      lstat(sourcePath),
      lstat(destinationPath),
    ]);

    if (
      !identityMatches(before, after)
      || !copied.isFile()
      || copied.isSymbolicLink()
      || copied.size !== before.size
      || (copied.mode & 0o077) !== 0
    ) {
      throw inputError();
    }

    const handle = await open(destinationPath, 'r');

    try {
      const header = Buffer.alloc(3);
      const { bytesRead } = await handle.read(header, 0, header.length, 0);

      if (
        bytesRead !== 3
        || header[0] !== 0xff
        || header[1] !== 0xd8
        || header[2] !== 0xff
      ) {
        throw inputError();
      }
    } finally {
      await handle.close().catch(() => {});
    }
  } catch (error) {
    if (error instanceof AppError) throw error;
    throw inputError();
  }
}

function buildEnvironment(sourceEnv, scratchPath) {
  const sourceHome = typeof sourceEnv.HOME === 'string' && path.isAbsolute(sourceEnv.HOME)
    ? sourceEnv.HOME
    : os.homedir();
  const codexHome = typeof sourceEnv.CODEX_HOME === 'string'
    && path.isAbsolute(sourceEnv.CODEX_HOME)
    ? sourceEnv.CODEX_HOME
    : path.join(sourceHome, '.codex');
  const environment = {
    HOME: scratchPath,
    TMPDIR: scratchPath,
    CODEX_HOME: codexHome,
    NO_COLOR: '1',
  };

  if (typeof sourceEnv.PATH === 'string') {
    environment.PATH = sanitizeExecutablePath(sourceEnv.PATH);
  }
  for (const key of ['LANG', 'LC_ALL']) {
    if (typeof sourceEnv[key] === 'string') environment[key] = sourceEnv[key];
  }

  return environment;
}

function buildArguments(scratchPath, schemaPath, resultPath, imagePaths) {
  const argumentsList = [
    'exec',
    '--strict-config',
    '--ignore-user-config',
    '--ignore-rules',
    '--ephemeral',
    '--skip-git-repo-check',
    '--sandbox',
    'read-only',
    '--color',
    'never',
    '-c',
    'skills.include_instructions=false',
  ];

  for (const feature of DISABLED_FEATURES) {
    argumentsList.push('--disable', feature);
  }

  argumentsList.push(
    '-C',
    scratchPath,
    '--output-schema',
    schemaPath,
    '-o',
    resultPath,
    ...imagePaths.map((imagePath) => `--image=${imagePath}`),
    '-',
  );

  return argumentsList;
}

function authFailure(stderr) {
  return /(?:codex\s+login|not\s+logged\s+in|authentication|unauthorized|\b401\b|missing\s+(?:bearer|credentials?))/iu.test(stderr);
}

async function runProcess({
  args,
  command,
  env,
  prompt,
  scratchPath,
  signal,
  spawnProcess,
  signalProcess,
  timeoutMs,
  killGraceMs,
  maxProcessOutputBytes,
}) {
  let child;

  try {
    child = spawnProcess(command, args, {
      cwd: scratchPath,
      detached: process.platform !== 'win32',
      env,
      shell: false,
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    });
  } catch (error) {
    throw new AppError(error?.code === 'ENOENT' ? 'CODEX_NOT_FOUND' : 'CODEX_EXECUTION_FAILED');
  }

  if (
    !child
    || !child.stdin
    || !child.stdout
    || !child.stderr
    || typeof child.once !== 'function'
  ) {
    throw new AppError('CODEX_EXECUTION_FAILED');
  }

  let closed = false;
  let exitCode;
  let exitSignal;
  let spawnError;
  let terminationReason;
  let killTimer;
  let timeoutTimer;
  let stderrSize = 0;
  let stdoutSize = 0;
  let stdioError = false;
  const stderrChunks = [];

  const sendSignal = (processSignal) => {
    let sent = false;

    if (process.platform !== 'win32' && Number.isSafeInteger(child.pid) && child.pid > 0) {
      try {
        signalProcess(-child.pid, processSignal);
        sent = true;
      } catch {
        sent = false;
      }
    }

    if (!sent && typeof child.kill === 'function') {
      try {
        child.kill(processSignal);
      } catch {
        // The close event remains the lifecycle authority.
      }
    }
  };

  const terminate = (reason) => {
    if (closed || terminationReason !== undefined) return;
    terminationReason = reason;
    sendSignal('SIGTERM');
    killTimer = setTimeout(() => {
      if (!closed) sendSignal('SIGKILL');
    }, killGraceMs);
    killTimer.unref?.();
  };

  const collect = (kind, chunk) => {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);

    if (kind === 'stderr') {
      stderrSize += buffer.length;
      if (stderrSize <= maxProcessOutputBytes) stderrChunks.push(buffer);
    } else {
      stdoutSize += buffer.length;
    }

    if (stderrSize > maxProcessOutputBytes || stdoutSize > maxProcessOutputBytes) {
      terminate('output');
    }
  };

  const onStdioError = () => {
    stdioError = true;
    terminate('stdio');
  };

  child.stdout.on('data', (chunk) => collect('stdout', chunk));
  child.stderr.on('data', (chunk) => collect('stderr', chunk));
  child.stdout.on('error', onStdioError);
  child.stderr.on('error', onStdioError);
  child.stdin.on('error', onStdioError);

  const closePromise = new Promise((resolve) => {
    child.once('error', (error) => {
      spawnError = error;
    });
    child.once('close', (code, processSignal) => {
      closed = true;
      exitCode = code;
      exitSignal = processSignal;
      resolve();
    });
  });

  const onAbort = () => terminate('cancelled');
  signal?.addEventListener('abort', onAbort, { once: true });
  if (signal?.aborted) terminate('cancelled');
  timeoutTimer = setTimeout(() => terminate('timeout'), timeoutMs);

  try {
    try {
      child.stdin.end(prompt, 'utf8');
    } catch {
      onStdioError();
    }
    await closePromise;
  } finally {
    clearTimeout(timeoutTimer);
    clearTimeout(killTimer);
    signal?.removeEventListener('abort', onAbort);
  }

  if (terminationReason === 'cancelled') throw new AppError('REQUEST_CANCELLED');
  if (terminationReason === 'timeout') throw new AppError('CODEX_TIMEOUT');
  if (terminationReason === 'output') throw new AppError('CODEX_OUTPUT_TOO_LARGE');
  if (terminationReason === 'stdio' || stdioError) throw new AppError('CODEX_EXECUTION_FAILED');
  if (spawnError) {
    throw new AppError(spawnError.code === 'ENOENT' ? 'CODEX_NOT_FOUND' : 'CODEX_EXECUTION_FAILED');
  }

  const stderr = Buffer.concat(stderrChunks).toString('utf8');
  if (exitCode !== 0 || exitSignal !== null) {
    throw new AppError(authFailure(stderr) ? 'CODEX_AUTH_REQUIRED' : 'CODEX_EXECUTION_FAILED');
  }
}

async function readResult(resultPath, maxResultBytes) {
  let handle;

  try {
    const pathStats = await lstat(resultPath);

    if (
      !pathStats.isFile()
      || pathStats.isSymbolicLink()
      || pathStats.size < 1
      || pathStats.size > maxResultBytes
    ) {
      throw new AppError(
        pathStats.size > maxResultBytes
          ? 'CODEX_OUTPUT_TOO_LARGE'
          : 'THREADS_COPY_OUTPUT_INVALID',
      );
    }

    await chmod(resultPath, PRIVATE_FILE_MODE);
    handle = await open(resultPath, 'r');
    const handleStats = await handle.stat();

    if (!handleStats.isFile() || !identityMatches(pathStats, handleStats)) {
      throw new AppError('THREADS_COPY_OUTPUT_INVALID');
    }

    const content = await handle.readFile({ encoding: 'utf8' });
    const finalStats = await handle.stat();

    if (
      !identityMatches(handleStats, finalStats)
      || finalStats.size !== Buffer.byteLength(content)
    ) {
      throw new AppError('THREADS_COPY_OUTPUT_INVALID');
    }

    let parsed;

    try {
      parsed = JSON.parse(content);
    } catch {
      throw new AppError('THREADS_COPY_OUTPUT_INVALID');
    }

    return validateThreadsCopyOutput(parsed);
  } catch (error) {
    if (error instanceof AppError) throw error;
    throw new AppError('THREADS_COPY_OUTPUT_INVALID');
  } finally {
    await handle?.close().catch(() => {});
  }
}

async function cleanupScratch(scratchPath, removeDirectory) {
  for (let attempt = 0; attempt < SCRATCH_CLEANUP_ATTEMPTS; attempt += 1) {
    try {
      await removeDirectory(scratchPath, { recursive: true, force: true });
    } catch {
      // Existence verification below decides whether another attempt is needed.
    }

    try {
      await lstat(scratchPath);
    } catch (error) {
      if (error?.code === 'ENOENT') return;
    }
  }

  throw new AppError('CODEX_CLEANUP_FAILED');
}

export async function runCodexThreadsCopy(
  input,
  {
    codexCommand = 'codex',
    killGraceMs = DEFAULT_CODEX_KILL_GRACE_MS,
    maxProcessOutputBytes = DEFAULT_CODEX_PROCESS_OUTPUT_BYTES,
    maxResultBytes = DEFAULT_CODEX_RESULT_BYTES,
    removeDirectory = rm,
    signalProcess = process.kill.bind(process),
    sourceEnv = process.env,
    spawnProcess = spawn,
    tempRoot = os.tmpdir(),
    timeoutMs = DEFAULT_CODEX_TIMEOUT_MS,
  } = {},
) {
  validateRunnerInput(input);
  const {
    analysisContext,
    imagePaths,
    signal,
    validationRetry = false,
  } = input;

  if (signal?.aborted) throw new AppError('REQUEST_CANCELLED');

  const options = {
    codexCommand,
    killGraceMs,
    maxProcessOutputBytes,
    maxResultBytes,
    removeDirectory,
    signalProcess,
    sourceEnv,
    spawnProcess,
    tempRoot,
    timeoutMs,
  };
  validateOptions(options);
  let scratchPath;

  try {
    scratchPath = await mkdtemp(path.join(tempRoot, 'rednote-codex-'));
    await chmod(scratchPath, SCRATCH_MODE);

    const copiedImages = [];

    for (let index = 0; index < imagePaths.length; index += 1) {
      if (signal?.aborted) throw new AppError('REQUEST_CANCELLED');
      const destination = path.join(scratchPath, `frame-${index + 1}.jpg`);
      await copyImageToScratch(imagePaths[index], destination);
      copiedImages.push(destination);
    }

    const schemaPath = path.join(scratchPath, 'threads-copy-schema.json');
    const resultPath = path.join(scratchPath, 'threads-copy-result.json');
    await writeFile(schemaPath, `${JSON.stringify(THREADS_COPY_SCHEMA)}\n`, {
      flag: 'wx',
      mode: PRIVATE_FILE_MODE,
    });
    const prompt = buildThreadsCopyPrompt(analysisContext, { validationRetry });
    const args = buildArguments(scratchPath, schemaPath, resultPath, copiedImages);
    const env = buildEnvironment(sourceEnv, scratchPath);

    if (signal?.aborted) throw new AppError('REQUEST_CANCELLED');

    await runProcess({
      args,
      command: codexCommand,
      env,
      prompt,
      scratchPath,
      signal,
      spawnProcess,
      signalProcess,
      timeoutMs,
      killGraceMs,
      maxProcessOutputBytes,
    });

    return await readResult(resultPath, maxResultBytes);
  } finally {
    if (scratchPath) {
      await cleanupScratch(scratchPath, removeDirectory);
    }
  }
}
