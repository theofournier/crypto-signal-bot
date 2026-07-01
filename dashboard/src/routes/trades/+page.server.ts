import type { PageServerLoad } from './$types';
import * as db from '$lib/server/db';

export const load: PageServerLoad = () => {
	return {
		stats: db.tradeStats(),
		equity: db.equityCurve(),
		trades: db.trades(500)
	};
};
