import type { Config } from 'tailwindcss';

const config: Config = {
	content: [
		'./app/**/*.{js,ts,jsx,tsx,mdx}',
		'./components/**/*.{js,ts,jsx,tsx,mdx}',
		'../../packages/shared/src/**/*.{js,ts,jsx,tsx,mdx}',
	],
	theme: {
		extend: {
			fontSize: {
				xs: ['0.8125rem', { lineHeight: '1.25rem' }],
				sm: ['0.9375rem', { lineHeight: '1.45rem' }],
				base: ['1rem', { lineHeight: '1.6rem' }],
				lg: ['1.125rem', { lineHeight: '1.65rem' }],
				xl: ['1.3125rem', { lineHeight: '1.75rem' }],
				'2xl': ['1.625rem', { lineHeight: '2rem' }],
				'3xl': ['2rem', { lineHeight: '2.3rem' }],
				'4xl': ['2.5rem', { lineHeight: '2.75rem' }],
			},
			fontFamily: {
				sans: ['var(--rf-font)', 'ui-sans-serif', 'system-ui', 'sans-serif'],
				hand: ['var(--rf-font-hand)', 'Comic Sans MS', 'cursive'],
			},
			ringColor: {
				DEFAULT: 'rgba(17, 17, 17, 0.16)',
			},
			boxShadow: {
				soft: '0 20px 50px rgba(0, 0, 0, 0.05)',
				lift: '0 24px 60px rgba(0, 0, 0, 0.09)',
			},
			borderRadius: {
				control: '999px',
				field: '18px',
				card: '28px',
				feature: '36px',
			},
			colors: {
				canvas: 'var(--rf-paper)',
				surface: 'var(--rf-paper)',
				ink: 'var(--rf-ink)',
				line: 'var(--rf-line)',
				action: 'var(--rf-ink)',
				danger: 'var(--rf-rose-deep)',
				fill: 'var(--rf-fill)',
				peach: { DEFAULT: '#ffe8d6', label: '#fed7aa', deep: '#9a3412', stroke: '#f97316' },
				mint: { DEFAULT: '#d7f1e1', label: '#bbf7d0', deep: '#166534', stroke: '#10b981' },
				sky: { DEFAULT: '#e0f2fe', label: '#bae6fd', deep: '#075985', stroke: '#0ea5e9' },
				lilac: { DEFAULT: '#f3e8ff', label: '#e9d5ff', deep: '#6b21a8', stroke: '#a855f7' },
				white: '#ffffff',
				// Legacy palette names, remapped so older utility classes follow the new look.
				slate: {
					50: '#fafafa', 100: '#f5f5f5', 200: '#ececec', 300: '#d9d9d9', 400: '#a3a3a3',
					500: '#7a7a7a', 600: '#5f5f5f', 700: '#454545', 800: '#2a2a2a', 900: '#111111', 950: '#0a0a0a',
				},
				emerald: {
					50: '#effaf3', 100: '#d7f1e1', 200: '#bbf7d0', 300: '#86efac', 400: '#34d399',
					500: '#10b981', 600: '#059669', 700: '#166534', 800: '#14532d', 900: '#0f3d22', 950: '#052e16',
				},
				teal: {
					50: '#f0f9ff', 100: '#e0f2fe', 200: '#bae6fd', 300: '#7dd3fc', 400: '#38bdf8',
					500: '#0ea5e9', 600: '#0284c7', 700: '#075985', 800: '#0c4a6e', 900: '#0b3b57', 950: '#082f49',
				},
				amber: {
					50: '#fff7ed', 100: '#ffe8d6', 200: '#fed7aa', 300: '#fdba74', 400: '#fb923c',
					500: '#f97316', 600: '#ea580c', 700: '#9a3412', 800: '#7c2d12', 900: '#5c2410', 950: '#431407',
				},
				rose: {
					50: '#fff1f2', 100: '#ffe4e6', 200: '#fecdd3', 300: '#fda4af', 400: '#fb7185',
					500: '#f43f5e', 600: '#e11d48', 700: '#9f1239', 800: '#881337', 900: '#6b0f2c', 950: '#4c0519',
				},
				red: {
					50: '#fff1f2', 100: '#ffe4e6', 200: '#fecdd3', 500: '#f43f5e', 600: '#e11d48', 700: '#9f1239', 800: '#881337',
				},
			},
		},
	},
	plugins: [],
};

export default config;
