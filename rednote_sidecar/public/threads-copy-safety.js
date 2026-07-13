const REDACTED_LINK = '[링크 제거됨]';
const REDACTED_CREDENTIAL = '[민감정보 제거됨]';

const SAFE_SNS_LABELS = new Set([
  'a', 'after', 'before', 'bonus', 'check', 'diy', 'faq', 'note',
  'fyi', 'point', 'pov', 'ps', 'q', 'review', 'step', 'tip', 'tmi', 'vs',
]);
const KNOWN_URI_SCHEMES = new Set([
  'about', 'bitcoin', 'blob', 'chrome', 'chrome-extension', 'data', 'facetime',
  'file', 'ftp', 'geo', 'git', 'git+ssh', 'http', 'https', 'intent', 'javascript',
  'magnet', 'mailto', 'sms', 'ssh', 'tel', 'urn', 'vbscript', 'view-source', 'ws', 'wss',
]);
const DOT_EQUIVALENTS = new Set(['\u3002', '\uff0e', '\uff61']);
const URL_IGNORED_WHITESPACE = new Set(['\t', '\n', '\r']);
const LABEL_CHARACTER = /[\p{L}\p{N}\p{M}-]/u;
const LABEL_EDGE_CHARACTER = /[\p{L}\p{N}\p{M}]/u;
const EXPLICIT_CREDENTIAL_SEPARATORS = Object.freeze([
  '->', '=>', '⇒', '⟶', '➜', '↦', '→', ':', '=',
]);
const AUTHORIZATION_SCHEMES = new Set(['basic', 'bearer']);
const CREDENTIAL_KEY_SUFFIXES = Object.freeze([
  'accesskey', 'apikey', 'cookie', 'credential', 'credentials', 'jwt',
  'passphrase', 'passwd', 'password', 'privatekey', 'secret', 'secretkey',
  'sessionid', 'sessionkey', 'signature', 'token', 'xsecsource',
]);

// Delegated ASCII TLDs from IANA's authoritative list, version 2026062302, Last Updated Wed Jun 24 07:07:01 2026 UTC.
// https://data.iana.org/TLD/tlds-alpha-by-domain.txt
const IANA_ASCII_TLDS = new Set(`
aaa aarp abb abbott abbvie abc able abogado
abudhabi ac academy accenture accountant accountants aco actor
ad ads adult ae aeg aero aetna af
afl africa ag agakhan agency ai aig airbus
airforce airtel akdn al alibaba alipay allfinanz allstate
ally alsace alstom am amazon americanexpress americanfamily amex
amfam amica amsterdam analytics android anquan anz ao
aol apartments app apple aq aquarelle ar arab
aramco archi army arpa art arte as asda
asia associates at athleta attorney au auction audi
audible audio auspost author auto autos aw aws
ax axa az azure ba baby baidu banamex
band bank bar barcelona barclaycard barclays barefoot bargains
baseball basketball bauhaus bayern bb bbc bbt bbva
bcg bcn bd be beats beauty beer berlin
best bestbuy bet bf bg bh bharti bi
bible bid bike bing bingo bio biz bj
black blackfriday blockbuster blog bloomberg blue bm bms
bmw bn bnpparibas bo boats boehringer bofa bom
bond boo book booking bosch bostik boston bot
boutique box br bradesco bridgestone broadway broker brother
brussels bs bt build builders business buy buzz
bv bw by bz bzh ca cab cafe
cal call calvinklein cam camera camp canon capetown
capital capitalone car caravan cards care career careers
cars casa case cash casino cat catering catholic
cba cbn cbre cc cd center ceo cern
cf cfa cfd cg ch chanel channel charity
chase chat cheap chintai christmas chrome church ci
cipriani circle cisco citadel citi citic city ck
cl claims cleaning click clinic clinique clothing cloud
club clubmed cm cn co coach codes coffee
college cologne com commbank community company compare computer
comsec condos construction consulting contact contractors cooking cool
coop corsica country coupon coupons courses cpa cr
credit creditcard creditunion cricket crown crs cruise cruises
cu cuisinella cv cw cx cy cymru cyou
cz dad dance data date dating datsun day
dclk dds de deal dealer deals degree delivery
dell deloitte delta democrat dental dentist desi design
dev dhl diamonds diet digital direct directory discount
discover dish diy dj dk dm dnp do
docs doctor dog domains dot download drive dtv
dubai dupont durban dvag dvr dz earth eat
ec eco edeka edu education ee eg email
emerck energy engineer engineering enterprises epson equipment er
ericsson erni es esq estate et eu eurovision
eus events exchange expert exposed express extraspace fage
fail fairwinds faith family fan fans farm farmers
fashion fast fedex feedback ferrari ferrero fi fidelity
fido film final finance financial fire firestone firmdale
fish fishing fit fitness fj fk flickr flights
flir florist flowers fly fm fo foo food
football ford forex forsale forum foundation fox fr
free fresenius frl frogans frontier ftr fujitsu fun
fund furniture futbol fyi ga gal gallery gallo
gallup game games gap garden gay gb gbiz
gd gdn ge gea gent genting george gf
gg ggee gh gi gift gifts gives giving
gl glass gle global globo gm gmail gmbh
gmo gmx gn godaddy gold goldpoint golf goodyear
goog google gop got gov gp gq gr
grainger graphics gratis green gripe grocery group gs
gt gu gucci guge guide guitars guru gw
gy hair hamburg hangout haus hbo hdfc hdfcbank
health healthcare help helsinki here hermes hiphop hisamitsu
hitachi hiv hk hkt hm hn hockey holdings
holiday homedepot homegoods homes homesense honda horse hospital
host hosting hot hotels hotmail house how hr
hsbc ht hu hughes hyatt hyundai ibm icbc
ice icu id ie ieee ifm ikano il
im imamat imdb immo immobilien in inc industries
infiniti info ing ink institute insurance insure int
international intuit investments io ipiranga iq ir irish
is ismaili ist istanbul it itau itv jaguar
java jcb je jeep jetzt jewelry jio jll
jm jmp jnj jo jobs joburg jot joy
jp jpmorgan jprs juegos juniper kaufen kddi ke
kerryhotels kerryproperties kfh kg kh ki kia kids
kim kindle kitchen kiwi km kn koeln komatsu
kosher kp kpmg kpn kr krd kred kuokgroup
kw ky kyoto kz la lacaixa lamborghini lamer
land landrover lanxess lasalle lat latino latrobe law
lawyer lb lc lds lease leclerc lefrak legal
lego lexus lgbt li lidl life lifeinsurance lifestyle
lighting like lilly limited limo lincoln link live
living lk llc llp loan loans locker locus
lol london lotte lotto love lpl lplfinancial lr
ls lt ltd ltda lu lundbeck luxe luxury
lv ly ma madrid maif maison makeup man
management mango map market marketing markets marriott marshalls
mattel mba mc mckinsey md me med media
meet melbourne meme memorial men menu merck merckmsd
mg mh miami microsoft mil mini mint mit
mitsubishi mk ml mlb mls mm mma mn
mo mobi mobile moda moe moi mom monash
money monster mormon mortgage moscow moto motorcycles mov
movie mp mq mr ms msd mt mtn
mtr mu museum music mv mw mx my
mz na nab nagoya name navy nba nc
ne nec net netbank netflix network neustar new
news next nextdirect nexus nf nfl ng ngo
nhk ni nico nike nikon ninja nissan nissay
nl no nokia norton now nowruz nowtv np
nr nra nrw ntt nu nyc nz obi
observer office okinawa olayan olayangroup ollo om omega
one ong onl online ooo open oracle orange
org organic origins osaka otsuka ott ovh pa
page panasonic paris pars partners parts party pay
pccw pe pet pf pfizer pg ph pharmacy
phd philips phone photo photography photos physio pics
pictet pictures pid pin ping pink pioneer pizza
pk pl place play playstation plumbing plus pm
pn pnc pohl poker politie porn post pr
praxi press prime pro prod productions prof progressive
promo properties property protection pru prudential ps pt
pub pw pwc py qa qpon quebec quest
racing radio re read realestate realtor realty recipes
red redumbrella rehab reise reisen reit reliance ren
rent rentals repair report republican rest restaurant review
reviews rexroth rich richardli ricoh ril rio rip
ro rocks rodeo rogers room rs rsvp ru
rugby ruhr run rw rwe ryukyu sa saarland
safe safety sakura sale salon samsclub samsung sandvik
sandvikcoromant sanofi sap sarl sas save saxo sb
sbi sbs sc scb schaeffler schmidt scholarships school
schule schwarz science scot sd se search seat
secure security seek select sener services seven sew
sex sexy sfr sg sh shangrila sharp shell
shia shiksha shoes shop shopping shouji show si
silk sina singles site sj sk ski skin
sky skype sl sling sm smart smile sn
sncf so soccer social softbank software sohu solar
solutions song sony soy spa space sport spot
sr srl ss st stada staples star statebank
statefarm stc stcgroup stockholm storage store stream studio
study style su sucks supplies supply support surf
surgery suzuki sv swatch swiss sx sy sydney
systems sz tab taipei talk taobao target tatamotors
tatar tattoo tax taxi tc tci td tdk
team tech technology tel temasek tennis teva tf
tg th thd theater theatre tiaa tickets tienda
tips tires tirol tj tjmaxx tjx tk tkmaxx
tl tm tmall tn to today tokyo tools
top toray toshiba total tours town toyota toys
tr trade trading training travel travelers travelersinsurance trust
trv tt tube tui tunes tushu tv tvs
tw tz ua ubank ubs ug uk unicom
university uno uol ups us uy uz va
vacations vana vanguard vc ve vegas ventures verisign
versicherung vet vg vi viajes video vig viking
villas vin vip virgin visa vision viva vivo
vlaanderen vn vodka volvo vote voting voto voyage
vu wales walmart walter wang wanggou watch watches
weather weatherchannel webcam weber website wed wedding weibo
weir wf whoswho wien wiki williamhill win windows
wine winners wme woodside work works world wow
ws wtc wtf xbox xerox xihuan xin xxx
xyz yachts yahoo yamaxun yandex ye yodobashi yoga
yokohama you youtube yt yun za zappos zara
zero zip zm zone zuerich zw
`.trim().split(/\s+/u));

// Snapshot of delegated A-label IDN TLDs from IANA's authoritative list, 2026-07-12.
// https://data.iana.org/TLD/tlds-alpha-by-domain.txt
const IANA_IDN_TLDS = new Set(`
xn--11b4c3d xn--1ck2e1b xn--1qqw23a xn--2scrj9c xn--30rr7y xn--3bst00m
xn--3ds443g xn--3e0b707e xn--3hcrj9c xn--3pxu8k xn--42c2d9a xn--45br5cyl
xn--45brj9c xn--45q11c xn--4dbrk0ce xn--4gbrim xn--54b7fta0cc xn--55qw42g
xn--55qx5d xn--5su34j936bgsg xn--5tzm5g xn--6frz82g xn--6qq986b3xl xn--80adxhks
xn--80ao21a xn--80aqecdr1a xn--80asehdb xn--80aswg xn--8y0a063a xn--90a3ac
xn--90ae xn--90ais xn--9dbq2a xn--9et52u xn--9krt00a xn--b4w605ferd
xn--bck1b9a5dre4c xn--c1avg xn--c2br7g xn--cck2b3b xn--cckwcxetd xn--cg4bki
xn--clchc0ea0b2g2a9gcd xn--czr694b xn--czrs0t xn--czru2d xn--d1acj3b xn--d1alf
xn--e1a4c xn--eckvdtc9d xn--efvy88h xn--fct429k xn--fhbei xn--fiq228c5hs
xn--fiq64b xn--fiqs8s xn--fiqz9s xn--fjq720a xn--flw351e xn--fpcrj9c3d
xn--fzc2c9e2c xn--fzys8d69uvgm xn--g2xx48c xn--gckr3f0f xn--gecrj9c xn--gk3at1e
xn--h2breg3eve xn--h2brj9c xn--h2brj9c8c xn--hxt814e xn--i1b6b1a6a2e xn--imr513n
xn--io0a7i xn--j1aef xn--j1amh xn--j6w193g xn--jlq480n2rg xn--jvr189m
xn--kcrx77d1x4a xn--kprw13d xn--kpry57d xn--kput3i xn--l1acc xn--lgbbat1ad8j
xn--mgb9awbf xn--mgba3a3ejt xn--mgba3a4f16a xn--mgba7c0bbn0a xn--mgbaam7a8h xn--mgbab2bd
xn--mgbah1a3hjkrd xn--mgbai9azgqp6j xn--mgbayh7gpa xn--mgbbh1a xn--mgbbh1a71e xn--mgbc0a9azcg
xn--mgbca7dzdo xn--mgbcpq6gpa1a xn--mgberp4a5d4ar xn--mgbgu82a xn--mgbi4ecexp xn--mgbpl2fh
xn--mgbt3dhd xn--mgbtx2b xn--mgbx4cd0ab xn--mix891f xn--mk1bu44c xn--mxtq1m
xn--ngbc5azd xn--ngbe9e0a xn--ngbrx xn--node xn--nqv7f xn--nqv7fs00ema
xn--nyqy26a xn--o3cw4h xn--ogbpf8fl xn--otu796d xn--p1acf xn--p1ai
xn--pgbs0dh xn--pssy2u xn--q7ce6a xn--q9jyb4c xn--qcka1pmc xn--qxa6a
xn--qxam xn--rhqv96g xn--rovu88b xn--rvc1e0am3e xn--s9brj9c xn--ses554g
xn--t60b56a xn--tckwe xn--tiq49xqyj xn--unup4y xn--vermgensberater-ctb xn--vermgensberatung-pwb
xn--vhquv xn--vuq861b xn--w4r85el8fhu5dnra xn--w4rs40l xn--wgbh1c xn--wgbl6a
xn--xhq521b xn--xkc2al3hye2a xn--xkc2dl3a5ee0h xn--y9a3aq xn--yfro4i67o xn--ygbi2ammx
xn--zfr164b
`.trim().split(/\s+/u));

function codePointLengthAt(value, index) {
  return value.codePointAt(index) > 0xffff ? 2 : 1;
}

function previousCodePointStart(value, index) {
  const previous = index - 1;
  if (
    previous > 0
    && value.charCodeAt(previous) >= 0xdc00
    && value.charCodeAt(previous) <= 0xdfff
    && value.charCodeAt(previous - 1) >= 0xd800
    && value.charCodeAt(previous - 1) <= 0xdbff
  ) {
    return previous - 1;
  }
  return previous;
}

function characterAt(value, index) {
  const codePoint = value.codePointAt(index);
  return codePoint === undefined ? undefined : String.fromCodePoint(codePoint);
}

function isWhitespace(character) {
  return character !== undefined && /\s/u.test(character);
}

function skipWhitespace(value, start) {
  let cursor = start;
  while (cursor < value.length && isWhitespace(characterAt(value, cursor))) {
    cursor += codePointLengthAt(value, cursor);
  }
  return cursor;
}

function createUrlProjection(value) {
  let text = '';
  const starts = [];
  const ends = [];

  for (let index = 0; index < value.length;) {
    const length = codePointLengthAt(value, index);
    const original = value.slice(index, index + length);
    index += length;
    if (URL_IGNORED_WHITESPACE.has(original)) continue;

    const normalized = DOT_EQUIVALENTS.has(original) ? '.' : original;
    text += normalized;
    for (let offset = 0; offset < normalized.length; offset += 1) {
      starts.push(index - length);
      ends.push(index);
    }
  }

  return { text, starts, ends, originalLength: value.length };
}

function projectedSpan(projection, start, end, replacement) {
  if (end <= start || start < 0 || start >= projection.starts.length) return null;
  return {
    start: projection.starts[start],
    end: end >= projection.text.length
      ? projection.originalLength
      : projection.ends[end - 1],
    replacement,
  };
}

function isAsciiLetter(character) {
  return typeof character === 'string' && /^[a-z]$/iu.test(character);
}

function isAsciiSchemeCharacter(character) {
  return typeof character === 'string' && /^[a-z0-9+.-]$/iu.test(character);
}

function isAsciiWordCharacter(character) {
  return typeof character === 'string' && /^[a-z0-9_]$/iu.test(character);
}

function readProjectedPayload(value, start) {
  let cursor = skipWhitespace(value, start);
  if (value[cursor] === '"' || value[cursor] === "'") {
    return readQuoted(value, cursor).end;
  }
  while (
    cursor < value.length
    && !isWhitespace(characterAt(value, cursor))
    && value[cursor] !== '"'
    && value[cursor] !== "'"
    && value[cursor] !== '<'
    && value[cursor] !== '>'
  ) {
    cursor += codePointLengthAt(value, cursor);
  }
  return cursor;
}

function schemeSpans(projection) {
  const spans = [];
  const { text } = projection;

  for (let index = 0; index < text.length;) {
    if (
      !isAsciiLetter(text[index])
      || (index > 0 && isAsciiWordCharacter(text[index - 1]))
    ) {
      index += 1;
      continue;
    }

    let cursor = index + 1;
    while (cursor < text.length && isAsciiSchemeCharacter(text[cursor])) cursor += 1;
    if (text[cursor] !== ':') {
      index += 1;
      continue;
    }

    const scheme = text.slice(index, cursor).toLowerCase();
    const next = text[cursor + 1];
    const safeLabel = SAFE_SNS_LABELS.has(scheme);
    const literalSpaceLabel = next === ' ' && !KNOWN_URI_SCHEMES.has(scheme);
    const unsafe = KNOWN_URI_SCHEMES.has(scheme)
      || (!safeLabel && !literalSpaceLabel && next !== undefined);

    if (unsafe) {
      const end = Math.max(cursor + 1, readProjectedPayload(text, cursor + 1));
      spans.push(projectedSpan(projection, index, end, REDACTED_LINK));
    }
    index = cursor + 1;
  }

  return spans.filter(Boolean);
}

function isLabelCharacter(value, index) {
  return index >= 0 && index < value.length && LABEL_CHARACTER.test(characterAt(value, index));
}

function leftLabelStart(value, dotIndex) {
  let cursor = dotIndex;
  let count = 0;
  while (cursor > 0) {
    const previous = previousCodePointStart(value, cursor);
    if (!isLabelCharacter(value, previous)) break;
    cursor = previous;
    count += 1;
    if (count > 63) return null;
  }
  if (cursor === dotIndex) return null;
  const first = characterAt(value, cursor);
  const last = characterAt(value, previousCodePointStart(value, dotIndex));
  return LABEL_EDGE_CHARACTER.test(first) && LABEL_EDGE_CHARACTER.test(last) ? cursor : null;
}

function rightLabelEnd(value, start) {
  let cursor = start;
  let count = 0;
  while (cursor < value.length && isLabelCharacter(value, cursor) && count < 72) {
    cursor += codePointLengthAt(value, cursor);
    count += 1;
  }
  return cursor;
}

const idnTldCache = new Map();

function isDelegatedIdnTld(value) {
  if (!/[^\x00-\x7f]/u.test(value)) return false;
  if (idnTldCache.has(value)) return idnTldCache.get(value);

  let delegated = false;
  try {
    const hostname = new URL(`https://x.${value}`).hostname.toLowerCase();
    delegated = IANA_IDN_TLDS.has(hostname.slice(hostname.lastIndexOf('.') + 1));
  } catch {
    delegated = false;
  }
  idnTldCache.set(value, delegated);
  return delegated;
}

function tldPrefixEnd(value, start, candidateEnd) {
  const candidate = value.slice(start, candidateEnd);
  const lower = candidate.toLowerCase();

  if (lower.startsWith('xn--')) {
    const match = /^[a-z0-9-]+/u.exec(lower)?.[0] ?? '';
    if (IANA_IDN_TLDS.has(match)) return start + match.length;
  }

  const ascii = /^[a-z]+/iu.exec(candidate)?.[0] ?? '';
  if (IANA_ASCII_TLDS.has(ascii.toLowerCase())) {
    const next = candidate[ascii.length];
    if (next === undefined || next.codePointAt(0) > 0x7f) return start + ascii.length;
  }

  const boundaries = [];
  for (let cursor = start; cursor < candidateEnd && boundaries.length < 69;) {
    cursor += codePointLengthAt(value, cursor);
    boundaries.push(cursor);
  }
  const minimumLength = Math.max(1, boundaries.length - 6);
  for (let length = Math.min(63, boundaries.length); length >= minimumLength; length -= 1) {
    const end = boundaries[length - 1];
    if (isDelegatedIdnTld(value.slice(start, end))) return end;
  }
  return null;
}

function isReferenceTerminator(character) {
  return character === undefined
    || isWhitespace(character)
    || character === '"'
    || character === "'"
    || character === '<'
    || character === '>';
}

function extendReferenceSuffix(value, start) {
  let cursor = start;
  if (value[cursor] === ':') {
    let portEnd = cursor + 1;
    while (portEnd < value.length && /\d/u.test(value[portEnd]) && portEnd - cursor <= 5) {
      portEnd += 1;
    }
    if (portEnd > cursor + 1) cursor = portEnd;
  }
  if (value[cursor] === '/' || value[cursor] === '?' || value[cursor] === '#') {
    cursor += 1;
    while (cursor < value.length && !isReferenceTerminator(characterAt(value, cursor))) {
      cursor += codePointLengthAt(value, cursor);
    }
  }
  return cursor;
}

function domainSpans(projection) {
  const spans = [];
  const { text } = projection;

  for (let dot = 0; dot < text.length; dot += 1) {
    if (text[dot] !== '.') continue;
    const start = leftLabelStart(text, dot);
    const rightStart = dot + 1;
    if (start === null || !LABEL_EDGE_CHARACTER.test(characterAt(text, rightStart))) continue;
    const candidateEnd = rightLabelEnd(text, rightStart);
    const tldEnd = tldPrefixEnd(text, rightStart, candidateEnd);
    if (tldEnd === null) continue;
    const leftLabel = text.slice(start, dot);
    const attachedSuffix = text.slice(tldEnd, candidateEnd);
    if (
      /^[가-힣]{2,}다$/u.test(leftLabel)
      && /^(?:에서|에게|한테|으로|은|는|이|가|을|를|와|과|도|만|의|로)$/u.test(attachedSuffix)
    ) {
      continue;
    }
    const end = extendReferenceSuffix(text, tldEnd);
    spans.push(projectedSpan(projection, start, end, REDACTED_LINK));
  }
  return spans.filter(Boolean);
}

function validIpv4(value) {
  const pieces = value.split('.');
  return pieces.length === 4
    && pieces.every((piece) => /^\d{1,3}$/u.test(piece) && Number(piece) <= 255);
}

function validIpv6(value) {
  if (!value.includes(':') || value.indexOf('::') !== value.lastIndexOf('::')) return false;
  const compressed = value.includes('::');
  const [left = '', right = ''] = compressed ? value.split('::') : [value, ''];
  const leftParts = left === '' ? [] : left.split(':');
  const rightParts = right === '' ? [] : right.split(':');
  if (leftParts.includes('') || rightParts.includes('')) return false;

  const all = [...leftParts, ...rightParts];
  let units = 0;
  for (let index = 0; index < all.length; index += 1) {
    const part = all[index];
    if (part.includes('.')) {
      if (index !== all.length - 1 || !validIpv4(part)) return false;
      units += 2;
    } else {
      if (!/^[0-9a-f]{1,4}$/iu.test(part)) return false;
      units += 1;
    }
  }
  return compressed ? units < 8 : units === 8;
}

function ipv4Spans(projection) {
  const spans = [];
  for (const match of projection.text.matchAll(/\d{1,3}(?:\.\d{1,3}){3}/gu)) {
    const start = match.index;
    const end = start + match[0].length;
    const before = projection.text[start - 1];
    const after = projection.text[end];
    if (
      (before !== undefined && /[\d.]/u.test(before))
      || (after !== undefined && /[\d.]/u.test(after))
      || !validIpv4(match[0])
    ) {
      continue;
    }
    spans.push(projectedSpan(
      projection,
      start,
      extendReferenceSuffix(projection.text, end),
      REDACTED_LINK,
    ));
  }
  return spans.filter(Boolean);
}

function ipv6Spans(projection) {
  const spans = [];
  const { text } = projection;

  for (const match of text.matchAll(/\[([0-9a-f:.]+)\]/giu)) {
    if (!validIpv6(match[1])) continue;
    const start = match.index;
    const end = start + match[0].length;
    spans.push(projectedSpan(
      projection,
      start,
      extendReferenceSuffix(text, end),
      REDACTED_LINK,
    ));
  }

  for (let index = 0; index < text.length;) {
    if (!/[0-9a-f:]/iu.test(text[index]) || (index > 0 && /[0-9a-f:.]/iu.test(text[index - 1]))) {
      index += 1;
      continue;
    }
    let end = index;
    while (end < text.length && /[0-9a-f:.]/iu.test(text[end])) end += 1;
    const candidate = text.slice(index, end);
    const bracketed = text[index - 1] === '[' && text[end] === ']';
    if (!bracketed && validIpv6(candidate)) {
      spans.push(projectedSpan(
        projection,
        index,
        extendReferenceSuffix(text, end),
        REDACTED_LINK,
      ));
    }
    index = Math.max(end, index + 1);
  }
  return spans.filter(Boolean);
}

function readQuoted(value, start) {
  const quote = value[start];
  let cursor = start + 1;
  let decoded = '';

  while (cursor < value.length) {
    const character = value[cursor];
    if (character === quote) return { end: cursor + 1, decoded, closed: true };
    if (character === '\\' && cursor + 1 < value.length) {
      const escaped = value[cursor + 1];
      if (escaped === 'u' && /^[0-9a-f]{4}$/iu.test(value.slice(cursor + 2, cursor + 6))) {
        decoded += String.fromCharCode(Number.parseInt(value.slice(cursor + 2, cursor + 6), 16));
        cursor += 6;
        continue;
      }
      const escapeValues = {
        b: '\b', f: '\f', n: '\n', r: '\r', t: '\t',
      };
      decoded += escapeValues[escaped] ?? escaped;
      cursor += 2;
      continue;
    }
    decoded += character;
    cursor += 1;
  }
  return { end: value.length, decoded, closed: false };
}

function normalizedCredentialKey(value) {
  return value.toLowerCase().replace(/[\s._-]/gu, '');
}

function credentialKind(value) {
  const normalized = normalizedCredentialKey(value);
  if (AUTHORIZATION_SCHEMES.has(normalized)) return 'authorization-scheme';
  if (normalized.includes('authorization')) return 'authorization';
  if (CREDENTIAL_KEY_SUFFIXES.some((marker) => normalized.includes(marker))) {
    return 'credential';
  }
  return null;
}

function explicitSeparatorLength(value, start) {
  for (const separator of EXPLICIT_CREDENTIAL_SEPARATORS) {
    if (value.startsWith(separator, start)) return separator.length;
  }
  return 0;
}

function startsCredentialLikeValue(value, start) {
  const character = characterAt(value, start);
  return character === '"'
    || character === "'"
    || (typeof character === 'string' && /^[a-z0-9+/=_-]$/iu.test(character));
}

function readCredentialValueEnd(value, start, authorization) {
  if (start >= value.length) return start;
  if (value[start] === '"' || value[start] === "'") return readQuoted(value, start).end;

  let cursor = start;
  while (
    cursor < value.length
    && (authorization || value[cursor] !== ',')
    && value[cursor] !== ';'
    && value[cursor] !== '}'
    && value[cursor] !== ']'
  ) {
    cursor += 1;
  }
  return cursor;
}

function credentialSpanForKey(value, start, afterKey, key) {
  const kind = credentialKind(key);
  if (kind === null) return null;
  const afterWhitespace = skipWhitespace(value, afterKey);
  const separator = explicitSeparatorLength(value, afterWhitespace);
  let valueStart;

  if (separator > 0) {
    valueStart = skipWhitespace(value, afterWhitespace + separator);
  } else if (
    afterWhitespace > afterKey
    && startsCredentialLikeValue(value, afterWhitespace)
  ) {
    valueStart = afterWhitespace;
  } else {
    return null;
  }

  return {
    start,
    end: Math.max(valueStart, readCredentialValueEnd(value, valueStart, kind === 'authorization')),
    replacement: REDACTED_CREDENTIAL,
  };
}

function credentialSpans(value) {
  const spans = [];

  for (let index = 0; index < value.length;) {
    const character = value[index];
    if (character === '"' || character === "'") {
      const quoted = readQuoted(value, index);
      if (quoted.closed) {
        const span = credentialSpanForKey(value, index, quoted.end, quoted.decoded);
        if (span) {
          spans.push(span);
          index = span.end;
          continue;
        }
      }
      index += 1;
      continue;
    }

    if (!/[a-z0-9_-]/iu.test(character)) {
      index += 1;
      continue;
    }
    let end = index + 1;
    while (end < value.length && /[a-z0-9_.-]/iu.test(value[end])) end += 1;
    const key = value.slice(index, end);
    const span = credentialSpanForKey(value, index, end, key);
    if (span) {
      spans.push(span);
      index = span.end;
    } else {
      index = end;
    }
  }
  return spans;
}

function forbiddenSpans(value) {
  const projection = createUrlProjection(value);
  return [
    ...schemeSpans(projection),
    ...domainSpans(projection),
    ...ipv4Spans(projection),
    ...ipv6Spans(projection),
    ...credentialSpans(value),
  ];
}

function mergeSpans(spans) {
  const sorted = spans
    .filter((span) => span && span.end > span.start)
    .sort((left, right) => left.start - right.start || left.end - right.end);
  const merged = [];
  for (const span of sorted) {
    const previous = merged.at(-1);
    if (previous && span.start < previous.end) {
      previous.end = Math.max(previous.end, span.end);
      if (span.replacement === REDACTED_CREDENTIAL) previous.replacement = REDACTED_CREDENTIAL;
    } else {
      merged.push({ ...span });
    }
  }
  return merged;
}

function redactSpans(value, spans) {
  const parts = [];
  let cursor = 0;
  for (const span of mergeSpans(spans)) {
    parts.push(value.slice(cursor, span.start), span.replacement);
    cursor = span.end;
  }
  parts.push(value.slice(cursor));
  return parts.join('');
}

export function containsForbiddenThreadsReference(value) {
  return typeof value === 'string' && forbiddenSpans(value).length > 0;
}

export function redactUntrustedThreadsSource(value) {
  if (typeof value !== 'string') return '';
  const spans = forbiddenSpans(value);
  if (spans.length === 0) return value;
  const redacted = redactSpans(value, spans);
  return forbiddenSpans(redacted).length === 0
    ? redacted
    : '[민감정보 또는 링크 제거됨]';
}
