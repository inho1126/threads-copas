const MAX_SAMPLE_COUNT = 24;
const MIN_FRAME_COUNT = 3;
const TARGET_FRAME_COUNT = 5;
const START_MARGIN_RATIO = 0.07;
const END_MARGIN_RATIO = 0.93;

function clamp01(value) {
  return Math.min(1, Math.max(0, value));
}

function requireFiniteNumber(value, name) {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new TypeError(`${name} must be a finite number`);
  }
}

function requireByteRgba(rgba, name = 'rgba') {
  if (!(rgba instanceof Uint8Array) && !(rgba instanceof Uint8ClampedArray)) {
    throw new TypeError(`${name} must be an unsigned byte array`);
  }
  if (rgba.length === 0 || rgba.length % 4 !== 0) {
    throw new RangeError(`${name} must contain complete RGBA pixels`);
  }
  return rgba;
}

function requireDimension(value, name) {
  requireFiniteNumber(value, name);
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new RangeError(`${name} must be a positive integer`);
  }
}

function requireFrame(rgba, width, height) {
  requireByteRgba(rgba);
  requireDimension(width, 'width');
  requireDimension(height, 'height');
  const pixelCount = width * height;
  if (!Number.isSafeInteger(pixelCount) || pixelCount > Number.MAX_SAFE_INTEGER / 4) {
    throw new RangeError('frame dimensions are too large');
  }
  if (rgba.length !== pixelCount * 4) {
    throw new RangeError('RGBA length does not match frame dimensions');
  }
  return pixelCount;
}

export function buildSampleTimes(durationSeconds, maxSamples = MAX_SAMPLE_COUNT) {
  requireFiniteNumber(durationSeconds, 'durationSeconds');
  requireFiniteNumber(maxSamples, 'maxSamples');
  if (durationSeconds <= 0) {
    throw new RangeError('durationSeconds must be positive');
  }
  if (!Number.isSafeInteger(maxSamples) || maxSamples < MIN_FRAME_COUNT) {
    throw new RangeError('maxSamples must be an integer of at least three');
  }

  const sampleLimit = Math.min(MAX_SAMPLE_COUNT, maxSamples);
  const sampleCount = Math.min(sampleLimit, Math.max(MIN_FRAME_COUNT, Math.ceil(durationSeconds * 2)));
  const firstAllowed = durationSeconds * START_MARGIN_RATIO;
  const lastAllowed = durationSeconds * END_MARGIN_RATIO;
  const interval = (lastAllowed - firstAllowed) / (sampleCount + 1);
  const times = Array.from(
    { length: sampleCount },
    (_, index) => firstAllowed + interval * (index + 1),
  );

  if (times.some((time, index) => (
    !Number.isFinite(time)
    || time <= firstAllowed
    || time >= lastAllowed
    || time >= durationSeconds
    || (index > 0 && time <= times[index - 1])
  ))) {
    throw new RangeError('durationSeconds is too short to sample safely');
  }
  return times;
}

export function analyzeFrame(rgba, width, height) {
  const pixelCount = requireFrame(rgba, width, height);
  const luminances = new Float64Array(pixelCount);
  let luminanceSum = 0;
  let squaredLuminanceSum = 0;
  let darkPixels = 0;
  let brightPixels = 0;

  for (let pixel = 0; pixel < pixelCount; pixel += 1) {
    const offset = pixel * 4;
    const luminance = (
      0.2126 * rgba[offset]
      + 0.7152 * rgba[offset + 1]
      + 0.0722 * rgba[offset + 2]
    ) / 255;
    luminances[pixel] = luminance;
    luminanceSum += luminance;
    squaredLuminanceSum += luminance * luminance;
    if (luminance <= 0.05) darkPixels += 1;
    if (luminance >= 0.95) brightPixels += 1;
  }

  const meanLuminance = clamp01(luminanceSum / pixelCount);
  const variance = Math.max(0, squaredLuminanceSum / pixelCount - meanLuminance ** 2);
  const contrast = clamp01(Math.sqrt(variance) / 0.5);
  const darkClippedRatio = clamp01(darkPixels / pixelCount);
  const brightClippedRatio = clamp01(brightPixels / pixelCount);
  let horizontalSum = 0;
  let verticalSum = 0;
  let horizontalPairs = 0;
  let verticalPairs = 0;

  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const pixel = y * width + x;
      if (x > 0) {
        horizontalSum += Math.abs(luminances[pixel] - luminances[pixel - 1]);
        horizontalPairs += 1;
      }
      if (y > 0) {
        verticalSum += Math.abs(luminances[pixel] - luminances[pixel - width]);
        verticalPairs += 1;
      }
    }
  }

  const horizontalEdgeEnergy = horizontalPairs === 0 ? 0 : clamp01(horizontalSum / horizontalPairs);
  const verticalEdgeEnergy = verticalPairs === 0 ? 0 : clamp01(verticalSum / verticalPairs);
  const sharpness = clamp01((horizontalEdgeEnergy + verticalEdgeEnergy) * 1.5);
  const meanBalance = clamp01(1 - Math.abs(meanLuminance - 0.5) * 2);
  const clippingRatio = clamp01(darkClippedRatio + brightClippedRatio);
  const exposureQuality = clamp01(meanBalance * (1 - 0.6 * clippingRatio));
  const baseQuality = clamp01(
    sharpness * 0.45
    + contrast * 0.25
    + exposureQuality * 0.3,
  );

  return Object.freeze({
    meanLuminance,
    contrast,
    darkClippedRatio,
    brightClippedRatio,
    horizontalEdgeEnergy,
    verticalEdgeEnergy,
    sharpness,
    exposureQuality,
    baseQuality,
  });
}

export function frameDifference(leftRgba, rightRgba) {
  requireByteRgba(leftRgba, 'leftRgba');
  requireByteRgba(rightRgba, 'rightRgba');
  if (leftRgba.length !== rightRgba.length) {
    throw new RangeError('RGBA buffers must have equal lengths');
  }

  const pixelCount = leftRgba.length / 4;
  let differenceSum = 0;
  for (let offset = 0; offset < leftRgba.length; offset += 4) {
    differenceSum += (
      0.2126 * Math.abs(leftRgba[offset] - rightRgba[offset])
      + 0.7152 * Math.abs(leftRgba[offset + 1] - rightRgba[offset + 1])
      + 0.0722 * Math.abs(leftRgba[offset + 2] - rightRgba[offset + 2])
    ) / 255;
  }

  const difference = clamp01(differenceSum / pixelCount);
  if (difference < Number.EPSILON) return 0;
  if (difference > 1 - Number.EPSILON * 4) return 1;
  return difference;
}

function readSelectionOptions(options) {
  if (options === null || typeof options !== 'object' || Array.isArray(options)) {
    throw new TypeError('options must be an object');
  }
  const nearDuplicateThreshold = options.nearDuplicateThreshold ?? 0.055;
  requireFiniteNumber(nearDuplicateThreshold, 'nearDuplicateThreshold');
  if (nearDuplicateThreshold < 0 || nearDuplicateThreshold > 1) {
    throw new RangeError('nearDuplicateThreshold must be between zero and one');
  }

  const minTimeGapSeconds = options.minTimeGapSeconds;
  if (minTimeGapSeconds !== undefined) {
    requireFiniteNumber(minTimeGapSeconds, 'minTimeGapSeconds');
    if (minTimeGapSeconds < 0) {
      throw new RangeError('minTimeGapSeconds must not be negative');
    }
  }
  return { nearDuplicateThreshold, minTimeGapSeconds };
}

function normalizeCandidates(candidates) {
  if (!Array.isArray(candidates)) {
    throw new TypeError('candidates must be an array');
  }
  let rgbaLength;
  const normalized = candidates.map((candidate, originalIndex) => {
    if (candidate === null || typeof candidate !== 'object' || Array.isArray(candidate)) {
      throw new TypeError('each candidate must be an object');
    }
    requireFiniteNumber(candidate.time, 'candidate.time');
    if (candidate.time < 0) {
      throw new RangeError('candidate.time must not be negative');
    }
    requireByteRgba(candidate.rgba, 'candidate.rgba');
    if (rgbaLength === undefined) rgbaLength = candidate.rgba.length;
    if (candidate.rgba.length !== rgbaLength) {
      throw new RangeError('candidate RGBA buffers must have equal lengths');
    }
    if (candidate.analysis === null || typeof candidate.analysis !== 'object') {
      throw new TypeError('candidate.analysis must be an object');
    }
    requireFiniteNumber(candidate.analysis.baseQuality, 'candidate.analysis.baseQuality');
    if (candidate.analysis.baseQuality < 0 || candidate.analysis.baseQuality > 1) {
      throw new RangeError('candidate.analysis.baseQuality must be normalized');
    }
    return {
      time: candidate.time,
      rgba: candidate.rgba,
      baseQuality: candidate.analysis.baseQuality,
      originalIndex,
      sceneDifference: 0,
    };
  });

  normalized.sort((left, right) => left.time - right.time || left.originalIndex - right.originalIndex);
  const uniqueTimes = [];
  for (const candidate of normalized) {
    const previous = uniqueTimes.at(-1);
    if (!previous || candidate.time !== previous.time) {
      uniqueTimes.push(candidate);
    } else if (candidate.baseQuality > previous.baseQuality) {
      uniqueTimes[uniqueTimes.length - 1] = candidate;
    }
  }
  return uniqueTimes;
}

function buildDifferenceMatrix(candidates) {
  const differences = Array.from(
    { length: candidates.length },
    () => new Float64Array(candidates.length),
  );
  for (let left = 0; left < candidates.length; left += 1) {
    candidates[left].selectionIndex = left;
    for (let right = left + 1; right < candidates.length; right += 1) {
      const difference = frameDifference(candidates[left].rgba, candidates[right].rgba);
      differences[left][right] = difference;
      differences[right][left] = difference;
    }
  }
  return differences;
}

function addSceneDifferences(candidates, differences) {
  for (let index = 0; index < candidates.length; index += 1) {
    let total = 0;
    let count = 0;
    if (index > 0) {
      total += differences[index][index - 1];
      count += 1;
    }
    if (index + 1 < candidates.length) {
      total += differences[index][index + 1];
      count += 1;
    }
    candidates[index].sceneDifference = count === 0 ? 0 : total / count;
  }
}

function temporalBonus(candidate, selected, firstTime, span) {
  if (span === 0) return selected.length === 0 ? 1 : 0;
  if (selected.length === 0) {
    const position = (candidate.time - firstTime) / span;
    return clamp01(1 - Math.abs(position - 0.5) * 2);
  }
  const nearestDistance = Math.min(...selected.map((entry) => Math.abs(candidate.time - entry.time)));
  return clamp01((nearestDistance * 2) / span);
}

function candidateScore(candidate, distribution) {
  return clamp01(
    candidate.baseQuality * 0.45
    + candidate.sceneDifference * 0.25
    + distribution * 0.3,
  );
}

function areCompatible(left, right, minGap, duplicateThreshold, differences) {
  return (
    Math.abs(left.time - right.time) >= minGap
    && differences[left.selectionIndex][right.selectionIndex] >= duplicateThreshold
  );
}

function greedySelect(candidates, limit, { minGap, duplicateThreshold }, differences) {
  const selected = [];
  const remaining = new Set(candidates);
  const firstTime = candidates[0]?.time ?? 0;
  const span = (candidates.at(-1)?.time ?? firstTime) - firstTime;

  while (selected.length < limit && remaining.size > 0) {
    let best;
    for (const candidate of remaining) {
      if (selected.some((entry) => (
        !areCompatible(candidate, entry, minGap, duplicateThreshold, differences)
      ))) continue;

      const distribution = temporalBonus(candidate, selected, firstTime, span);
      const score = candidateScore(candidate, distribution);
      if (
        !best
        || score > best.score
        || (score === best.score && candidate.time < best.candidate.time)
        || (
          score === best.score
          && candidate.time === best.candidate.time
          && candidate.originalIndex < best.candidate.originalIndex
        )
      ) {
        best = { candidate, score };
      }
    }
    if (!best) break;
    selected.push({ ...best.candidate, score: best.score });
    remaining.delete(best.candidate);
  }
  return selected;
}

function compareTimestampSets(left, right) {
  for (let index = 0; index < left.length; index += 1) {
    if (left[index].time !== right[index].time) {
      return left[index].time - right[index].time;
    }
  }
  return 0;
}

function scoreCompatibleSet(selected, firstTime, span) {
  const scored = selected.map((candidate) => {
    const others = selected.filter((entry) => entry !== candidate);
    const distribution = temporalBonus(candidate, others, firstTime, span);
    return { ...candidate, score: candidateScore(candidate, distribution) };
  });
  return {
    selected: scored,
    aggregateScore: scored.reduce((total, candidate) => total + candidate.score, 0),
  };
}

function findBestCompatibleSet(candidates, limit, constraints, differences) {
  if (candidates.length < limit) return null;
  const firstTime = candidates[0].time;
  const span = candidates.at(-1).time - firstTime;
  let best;
  const selected = [];

  function visit(startIndex) {
    if (selected.length === limit) {
      const result = scoreCompatibleSet(selected, firstTime, span);
      if (
        !best
        || result.aggregateScore > best.aggregateScore + Number.EPSILON
        || (
          Math.abs(result.aggregateScore - best.aggregateScore) <= Number.EPSILON
          && compareTimestampSets(result.selected, best.selected) < 0
        )
      ) {
        best = result;
      }
      return;
    }

    const needed = limit - selected.length;
    const lastStart = candidates.length - needed;
    for (let index = startIndex; index <= lastStart; index += 1) {
      const candidate = candidates[index];
      if (selected.some((entry) => (
        !areCompatible(
          candidate,
          entry,
          constraints.minGap,
          constraints.duplicateThreshold,
          differences,
        )
      ))) continue;
      selected.push(candidate);
      visit(index + 1);
      selected.pop();
    }
  }

  visit(0);
  return best?.selected ?? null;
}

/**
 * Selects representative timestamps and returns chronological `{ time, score }`
 * records. Pixel buffers remain internal and are never returned.
 */
export function selectRepresentativeFrames(candidates, options = {}) {
  const { nearDuplicateThreshold, minTimeGapSeconds } = readSelectionOptions(options);
  const normalized = normalizeCandidates(candidates);
  if (normalized.length < MIN_FRAME_COUNT) {
    throw new RangeError('at least three unique candidates are required');
  }
  const differences = buildDifferenceMatrix(normalized);
  addSceneDifferences(normalized, differences);

  const span = normalized.at(-1).time - normalized[0].time;
  const strictGap = minTimeGapSeconds ?? span / (TARGET_FRAME_COUNT * 2);
  const strictConstraints = {
    minGap: strictGap,
    duplicateThreshold: nearDuplicateThreshold,
  };
  let selected = greedySelect(
    normalized,
    TARGET_FRAME_COUNT,
    strictConstraints,
    differences,
  );

  if (selected.length !== TARGET_FRAME_COUNT) {
    selected = findBestCompatibleSet(
      normalized,
      TARGET_FRAME_COUNT,
      strictConstraints,
      differences,
    ) ?? selected;
  }

  if (selected.length !== TARGET_FRAME_COUNT) {
    selected = greedySelect(normalized, Math.min(MIN_FRAME_COUNT, normalized.length), {
      minGap: 0,
      duplicateThreshold: 0,
    }, differences);
  }

  return selected
    .sort((left, right) => left.time - right.time || left.originalIndex - right.originalIndex)
    .map(({ time, score }) => Object.freeze({
      time,
      score: Number(score.toFixed(6)),
    }));
}
