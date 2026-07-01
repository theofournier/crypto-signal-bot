/** Resolve the `from`/`to` query params into a concrete, clamped day window. */
import { tsToDay, dayStartTs, dayEndTs, addDays, clampDay } from '$lib/daterange';

export interface ResolvedRange {
	from: string; // YYYY-MM-DD (inclusive)
	to: string; // YYYY-MM-DD (inclusive)
	min: string; // earliest day with data
	max: string; // latest day with data
	fromTs: number; // unix seconds, start of `from`
	toTs: number; // unix seconds, end of `to`
}

/**
 * Reads `?from=&to=` from the URL, falling back to the last `defaultSpanDays`
 * ending at the newest data. Everything is clamped into the table's real
 * [min, max] span so the filter can never point at empty ranges.
 */
export function resolveRange(
	url: URL,
	bounds: { min_ts: number | null; max_ts: number | null },
	defaultSpanDays = 14
): ResolvedRange | null {
	if (bounds.min_ts == null || bounds.max_ts == null) return null;
	const min = tsToDay(bounds.min_ts);
	const max = tsToDay(bounds.max_ts);

	let to = url.searchParams.get('to') ?? max;
	let from = url.searchParams.get('from') ?? addDays(to, -(defaultSpanDays - 1));

	from = clampDay(from, min, max);
	to = clampDay(to, min, max);
	if (from > to) [from, to] = [to, from];

	return { from, to, min, max, fromTs: dayStartTs(from), toTs: dayEndTs(to) };
}
