const P = { width: 16, height: 16, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 2, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const }

export const IconGrid = () => (<svg {...P}><rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" /><rect x="3" y="14" width="7" height="7" /><rect x="14" y="14" width="7" height="7" /></svg>)
export const IconServer = () => (<svg {...P}><rect x="3" y="4" width="18" height="7" rx="1" /><rect x="3" y="13" width="18" height="7" rx="1" /><path d="M7 7.5h.01M7 16.5h.01" /></svg>)
export const IconList = () => (<svg {...P}><path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01" /></svg>)
export const IconImage = () => (<svg {...P}><rect x="3" y="3" width="18" height="18" rx="2" /><circle cx="9" cy="9" r="2" /><path d="M21 15l-5-5L5 21" /></svg>)
export const IconChart = () => (<svg {...P}><path d="M18 20V10M12 20V4M6 20v-6" /></svg>)
export const IconPlus = () => (<svg {...P} width={14} height={14}><path d="M12 5v14M5 12h14" /></svg>)
export const IconRefresh = () => (<svg {...P} width={14} height={14}><path d="M21 12a9 9 0 1 1-3-6.7L21 8" /><path d="M21 3v5h-5" /></svg>)
export const IconSearch = () => (<svg {...P} width={14} height={14} stroke="#5E646D"><circle cx="11" cy="11" r="7" /><path d="M20 20l-3.5-3.5" /></svg>)
export const IconClose = () => (<svg {...P}><path d="M6 6l12 12M18 6L6 18" /></svg>)
