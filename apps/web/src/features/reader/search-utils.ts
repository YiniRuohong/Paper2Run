/* Fuzzy keyword matching + time formatting helpers for the example app. */

const normalize = (value: string) =>
  value.toLowerCase().replace(/\s+/g, " ").trim();

/**
 * Fuzzy-match one keyword against a text.
 * Substring hits score high (earlier = better); contiguous subsequence
 * hits score lower; no match returns -1.
 */
function keywordScore(text: string, keyword: string): number {
  const t = normalize(text);
  const k = normalize(keyword);
  if (!k) return 0;
  const idx = t.indexOf(k);
  if (idx >= 0) {
    return 100 - Math.min(idx, 40);
  }
  let from = 0;
  let score = 0;
  for (const ch of k) {
    const found = t.indexOf(ch, from);
    if (found < 0) {
      return -1;
    }
    score += found === from ? 2 : 1;
    from = found + 1;
  }
  return Math.min(score, 60);
}

/** All keywords (space separated, AND) must match. Returns summed score or -1. */
export function fuzzyMatch(text: string, query: string): number {
  const keywords = normalize(query).split(" ").filter(Boolean);
  if (keywords.length === 0) {
    return 0;
  }
  let total = 0;
  for (const keyword of keywords) {
    const score = keywordScore(text, keyword);
    if (score < 0) {
      return -1;
    }
    total += score;
  }
  return total;
}

/** First substring occurrence of any keyword (for snippet highlighting). */
export function firstMatchIndex(text: string, query: string): number {
  const t = normalize(text);
  let best = -1;
  for (const keyword of normalize(query).split(" ").filter(Boolean)) {
    const idx = t.indexOf(keyword);
    if (idx >= 0 && (best < 0 || idx < best)) {
      best = idx;
    }
  }
  return best;
}

/** Context window around `center` with an ellipsis on trimmed sides. */
export function snippetAround(
  text: string,
  center: number,
  radius = 26,
): string {
  const clean = text.replace(/\s+/g, " ").trim();
  const start = Math.max(
    0,
    Math.min(center - radius, Math.max(0, clean.length - radius * 2)),
  );
  const end = Math.min(clean.length, start + radius * 2);
  return `${start > 0 ? "…" : ""}${clean.slice(start, end)}${end < clean.length ? "…" : ""}`;
}

export function formatTime(timestamp: number): string {
  const delta = Date.now() - timestamp;
  if (delta < 60_000) return "刚刚";
  if (delta < 3_600_000) return `${Math.floor(delta / 60_000)} 分钟前`;
  if (delta < 86_400_000) return `${Math.floor(delta / 3_600_000)} 小时前`;
  const d = new Date(timestamp);
  const hhmm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  if (delta < 30 * 86_400_000)
    return `${d.getMonth() + 1}月${d.getDate()}日 ${hhmm}`;
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
