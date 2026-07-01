<script lang="ts">
	import '../app.css';
	import favicon from '$lib/assets/favicon.svg';
	import { page } from '$app/state';

	let { children } = $props();

	const links = [
		{ href: '/', label: 'Overview' },
		{ href: '/market', label: 'Market' },
		{ href: '/signals', label: 'Signals' },
		{ href: '/sentiment', label: 'Sentiment' },
		{ href: '/onchain', label: 'On-chain' },
		{ href: '/trades', label: 'Trades' }
	];

	function active(href: string): boolean {
		return href === '/' ? page.url.pathname === '/' : page.url.pathname.startsWith(href);
	}
</script>

<svelte:head>
	<link rel="icon" href={favicon} />
	<title>Crypto Signal Bot — Dashboard</title>
</svelte:head>

<header>
	<div class="bar">
		<a class="brand" href="/">🛰️ Crypto Signal Bot</a>
		<nav>
			{#each links as l (l.href)}
				<a href={l.href} class:active={active(l.href)}>{l.label}</a>
			{/each}
		</nav>
	</div>
</header>

{@render children()}

<footer>
	Read-only dashboard over <span class="mono">storage.db</span>. Educational software — dry-run by
	default, not financial advice.
</footer>

<style>
	header {
		position: sticky;
		top: 0;
		z-index: 10;
		background: var(--bg-elev);
		border-bottom: 1px solid var(--border);
	}
	.bar {
		max-width: 1200px;
		margin: 0 auto;
		padding: 0.6rem 1.5rem;
		display: flex;
		align-items: center;
		gap: 1.5rem;
		flex-wrap: wrap;
	}
	.brand {
		font-weight: 700;
		color: var(--text);
	}
	.brand:hover {
		text-decoration: none;
	}
	nav {
		display: flex;
		gap: 0.35rem;
		flex-wrap: wrap;
	}
	nav a {
		color: var(--muted);
		padding: 0.3rem 0.7rem;
		border-radius: 6px;
	}
	nav a:hover {
		color: var(--text);
		background: var(--bg-elev2);
		text-decoration: none;
	}
	nav a.active {
		color: var(--text);
		background: color-mix(in srgb, var(--accent) 20%, transparent);
	}
	footer {
		max-width: 1200px;
		margin: 2rem auto 1rem;
		padding: 1rem 1.5rem;
		color: var(--muted);
		font-size: 0.8rem;
		border-top: 1px solid var(--border);
	}
</style>
