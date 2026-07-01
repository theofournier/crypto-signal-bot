import type { PageServerLoad } from './$types';
import * as db from '$lib/server/db';
import { resolveRange } from '$lib/server/range';

export const load: PageServerLoad = ({ url }) => {
	const symbols = db.symbols();
	const symbol = url.searchParams.get('symbol') ?? symbols[0] ?? null;
	const range = resolveRange(url, db.tableRange('onchain_data'), 30);
	const rows = symbol && range ? db.onchain(symbol, { from: range.fromTs, to: range.toTs }) : [];
	return {
		symbols,
		symbol,
		range,
		distribution: db.flowDistribution(),
		rows
	};
};
