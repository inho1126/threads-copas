import { lookup as dnsLookup } from 'node:dns/promises';
import { isIP } from 'node:net';

export const MAX_REDIRECTS = 3;

const MEDIA_BASE_HOSTS = ['rednotecdn.com', 'xhscdn.com'];
const HOST_LABEL = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
const NETWORK_POLICY_ERROR = '허용되지 않은 네트워크 대상입니다.';

function hasValidHostnameLabels(hostname) {
  return hostname.length <= 253
    && hostname.split('.').every((label) => HOST_LABEL.test(label));
}

export function classifyHostname(hostname) {
  if (typeof hostname !== 'string') {
    return null;
  }

  const normalized = hostname.toLowerCase();

  if (normalized === 'www.rednote.com') {
    return 'page';
  }

  if (!hasValidHostnameLabels(normalized)) {
    return null;
  }

  return MEDIA_BASE_HOSTS.some((base) => normalized.endsWith(`.${base}`))
    ? 'media'
    : null;
}

export function classifyUrl(input) {
  if (typeof input !== 'string' && !(input instanceof URL)) {
    return null;
  }

  let url;

  try {
    url = new URL(input instanceof URL ? input.href : input);
  } catch {
    return null;
  }

  if (
    url.protocol !== 'https:'
    || url.port !== ''
    || url.username !== ''
    || url.password !== ''
    || url.hash !== ''
  ) {
    return null;
  }

  return classifyHostname(url.hostname);
}

function parseIpv4(address) {
  if (isIP(address) !== 4) {
    return null;
  }

  return address.split('.').map(Number);
}

function parseIpv6(address) {
  if (isIP(address) !== 6 || address.includes('%')) {
    return null;
  }

  let normalized = address.toLowerCase();

  if (normalized.includes('.')) {
    const separator = normalized.lastIndexOf(':');
    const ipv4 = parseIpv4(normalized.slice(separator + 1));

    if (!ipv4) {
      return null;
    }

    const high = ((ipv4[0] << 8) | ipv4[1]).toString(16);
    const low = ((ipv4[2] << 8) | ipv4[3]).toString(16);
    normalized = `${normalized.slice(0, separator)}:${high}:${low}`;
  }

  const halves = normalized.split('::');

  if (halves.length > 2) {
    return null;
  }

  const left = halves[0] ? halves[0].split(':') : [];
  const right = halves.length === 2 && halves[1] ? halves[1].split(':') : [];
  const missing = 8 - left.length - right.length;

  if ((halves.length === 1 && missing !== 0) || (halves.length === 2 && missing < 1)) {
    return null;
  }

  const words = [
    ...left,
    ...Array.from({ length: missing }, () => '0'),
    ...right,
  ].map((part) => Number.parseInt(part, 16));

  if (words.length !== 8 || words.some((word) => !Number.isInteger(word) || word < 0 || word > 0xffff)) {
    return null;
  }

  return words.flatMap((word) => [word >> 8, word & 0xff]);
}

function createCidr(address, prefixLength) {
  const bytes = parseIpv4(address) ?? parseIpv6(address);
  return Object.freeze({ bytes: Object.freeze(bytes), prefixLength });
}

function matchesCidr(bytes, cidr) {
  if (bytes.length !== cidr.bytes.length) {
    return false;
  }

  const fullBytes = Math.floor(cidr.prefixLength / 8);
  const remainingBits = cidr.prefixLength % 8;

  for (let index = 0; index < fullBytes; index += 1) {
    if (bytes[index] !== cidr.bytes[index]) {
      return false;
    }
  }

  if (remainingBits === 0) {
    return true;
  }

  const mask = (0xff << (8 - remainingBits)) & 0xff;
  return (bytes[fullBytes] & mask) === (cidr.bytes[fullBytes] & mask);
}

const IPV4_NON_GLOBAL_CIDRS = Object.freeze([
  createCidr('0.0.0.0', 8),
  createCidr('10.0.0.0', 8),
  createCidr('100.64.0.0', 10),
  createCidr('127.0.0.0', 8),
  createCidr('169.254.0.0', 16),
  createCidr('172.16.0.0', 12),
  createCidr('192.0.0.0', 24),
  createCidr('192.0.2.0', 24),
  createCidr('192.88.99.0', 24),
  createCidr('192.168.0.0', 16),
  createCidr('198.18.0.0', 15),
  createCidr('198.51.100.0', 24),
  createCidr('203.0.113.0', 24),
  createCidr('224.0.0.0', 4),
  createCidr('240.0.0.0', 4),
]);

const IPV6_GLOBAL_UNICAST = createCidr('2000::', 3);
const IPV6_NON_GLOBAL_CIDRS = Object.freeze([
  createCidr('2001::', 32),
  createCidr('2001:2::', 48),
  createCidr('2001:10::', 28),
  createCidr('2001:20::', 28),
  createCidr('2001:db8::', 32),
  createCidr('2002::', 16),
  createCidr('3fff::', 20),
  createCidr('fec0::', 10),
  createCidr('ff00::', 8),
]);

function isPublicIpv4(bytes) {
  return !IPV4_NON_GLOBAL_CIDRS.some((cidr) => matchesCidr(bytes, cidr));
}

export function isPublicIpAddress(address) {
  if (typeof address !== 'string') {
    return false;
  }

  const ipv4 = parseIpv4(address);

  if (ipv4) {
    return isPublicIpv4(ipv4);
  }

  const ipv6 = parseIpv6(address);

  if (!ipv6) {
    return false;
  }

  const isIpv4Mapped = ipv6.slice(0, 10).every((byte) => byte === 0)
    && ipv6[10] === 0xff
    && ipv6[11] === 0xff;

  if (isIpv4Mapped) {
    return isPublicIpv4(ipv6.slice(12));
  }

  return matchesCidr(ipv6, IPV6_GLOBAL_UNICAST)
    && !IPV6_NON_GLOBAL_CIDRS.some((cidr) => matchesCidr(ipv6, cidr));
}

export async function validateDns(hostname, { lookup = dnsLookup } = {}) {
  if (typeof hostname !== 'string' || typeof lookup !== 'function') {
    throw new TypeError(NETWORK_POLICY_ERROR);
  }

  const answers = await lookup(hostname, { all: true });

  if (!Array.isArray(answers) || answers.length === 0) {
    throw new TypeError(NETWORK_POLICY_ERROR);
  }

  const snapshot = answers.map((answer) => ({
    address: answer?.address,
    family: answer?.family,
  }));

  if (snapshot.some((answer) => (
    (answer.family !== 4 && answer.family !== 6)
    || isIP(answer.address) !== answer.family
    || !isPublicIpAddress(answer.address)
  ))) {
    throw new TypeError(NETWORK_POLICY_ERROR);
  }

  return Object.freeze(snapshot.map((answer) => Object.freeze(answer)));
}

function createPinnedLookup(hostname, addresses) {
  return function pinnedLookup(requestedHostname, options, callback) {
    let requestOptions = options;
    let done = callback;

    if (typeof options === 'function') {
      requestOptions = {};
      done = options;
    }

    if (typeof done !== 'function') {
      throw new TypeError('DNS lookup callback is required.');
    }

    queueMicrotask(() => {
      try {
        if (
          typeof requestedHostname !== 'string'
          || requestedHostname.toLowerCase() !== hostname
        ) {
          throw new TypeError(NETWORK_POLICY_ERROR);
        }

        const family = typeof requestOptions === 'number'
          ? requestOptions
          : (requestOptions?.family ?? 0);
        const all = typeof requestOptions === 'object' && requestOptions?.all === true;

        if (family !== 0 && family !== 4 && family !== 6) {
          throw new TypeError(NETWORK_POLICY_ERROR);
        }

        const eligible = family === 0
          ? addresses
          : addresses.filter((answer) => answer.family === family);

        if (eligible.length === 0) {
          throw new TypeError(NETWORK_POLICY_ERROR);
        }

        if (all) {
          done(null, eligible.map(({ address, family: answerFamily }) => ({
            address,
            family: answerFamily,
          })));
          return;
        }

        done(null, eligible[0].address, eligible[0].family);
      } catch (error) {
        done(error);
      }
    });
  };
}

export async function validateNetworkUrl(input, { kind, lookup = dnsLookup } = {}) {
  if ((kind !== 'page' && kind !== 'media') || classifyUrl(input) !== kind) {
    throw new TypeError(NETWORK_POLICY_ERROR);
  }

  const url = new URL(input instanceof URL ? input.href : input);
  const addresses = await validateDns(url.hostname, { lookup });

  return Object.freeze({
    href: url.href,
    hostname: url.hostname,
    addresses,
    lookup: createPinnedLookup(url.hostname, addresses),
  });
}

export async function validateRedirect(
  input,
  { kind, redirectCount, lookup = dnsLookup } = {},
) {
  if (!Number.isInteger(redirectCount) || redirectCount < 1 || redirectCount > MAX_REDIRECTS) {
    throw new TypeError(NETWORK_POLICY_ERROR);
  }

  return validateNetworkUrl(input, { kind, lookup });
}
