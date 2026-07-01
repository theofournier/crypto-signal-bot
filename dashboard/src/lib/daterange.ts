/** UTC day-string (YYYY-MM-DD) helpers shared by the date filter (client + server). */

export function tsToDay(ts: number): string {
	return new Date(ts * 1000).toISOString().slice(0, 10);
}

/** Unix seconds at 00:00:00Z of the given day. */
export function dayStartTs(day: string): number {
	return Math.floor(Date.parse(`${day}T00:00:00Z`) / 1000);
}

/** Unix seconds at 23:59:59Z of the given day (inclusive upper bound). */
export function dayEndTs(day: string): number {
	return Math.floor(Date.parse(`${day}T23:59:59Z`) / 1000);
}

export function addDays(day: string, n: number): string {
	const d = new Date(`${day}T00:00:00Z`);
	d.setUTCDate(d.getUTCDate() + n);
	return d.toISOString().slice(0, 10);
}

/** Whole days between two day-strings (to − from). */
export function daysBetween(from: string, to: string): number {
	return Math.round((Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86_400_000);
}

/** Clamp a day-string into [min, max]. */
export function clampDay(day: string, min: string, max: string): string {
	if (day < min) return min;
	if (day > max) return max;
	return day;
}
