import { atom } from 'nanostores';
import type { Currency } from '../format.ts';

// Estado compartilhado do Mercado entre os dois islands (filtros na sidebar ↔
// tabela no main). nanostores = singleton de módulo, então ambos veem o mesmo
// estado. Inicializa da URL e persiste na URL (replaceState).
const sp = () => new URLSearchParams(typeof location !== 'undefined' ? location.search : '');
const csv = (v: string | null) => (v ? v.split(',').filter(Boolean) : []);
const p0 = sp();

export const qA = atom<string>(p0.get('q') ?? '');
export const gradeA = atom<string[]>(csv(p0.get('grade')));
export const typeA = atom<string[]>(csv(p0.get('type')));
export const gtypeA = atom<string[]>(csv(p0.get('gt')));
export const clsA = atom<string[]>(csv(p0.get('cls')));
export const attrA = atom<string[]>(csv(p0.get('attr')));
export const lvlMinA = atom<string>(p0.get('lmin') ?? '');
export const lvlMaxA = atom<string>(p0.get('lmax') ?? '');
export const goldMinA = atom<string>(p0.get('gmin') ?? '');
export const goldMaxA = atom<string>(p0.get('gmax') ?? '');
export const priceMinA = atom<string>(p0.get('pmin') ?? '');
export const priceMaxA = atom<string>(p0.get('pmax') ?? '');
export const onlyTradA = atom<boolean>(p0.get('trad') === '1');
export const onlyBookA = atom<boolean>(p0.get('book') === '1');
export const onlyFavA = atom<boolean>(p0.get('fav') === '1');
export const curA = atom<Currency>(p0.get('cur') === 'usd' ? 'usd' : 'brl');
export const sortKeyA = atom<string>(p0.get('sort') || 'goldPer');
export const sortDirA = atom<'asc' | 'desc'>(p0.get('dir') === 'asc' ? 'asc' : 'desc');
export const viewA = atom<'table' | 'cards'>(p0.get('view') === 'table' ? 'table' : 'cards');

// favoritos persistidos
const FAV_KEY = 'tbh:favs';
const loadFavs = (): string[] => {
  try {
    return typeof localStorage !== 'undefined' ? JSON.parse(localStorage.getItem(FAV_KEY) || '[]') : [];
  } catch {
    return [];
  }
};
export const favsA = atom<string[]>(loadFavs());
export function toggleFav(name: string) {
  const cur = favsA.get();
  const next = cur.includes(name) ? cur.filter((n) => n !== name) : [...cur, name];
  favsA.set(next);
  if (typeof localStorage !== 'undefined') localStorage.setItem(FAV_KEY, JSON.stringify(next));
}

export function clearFilters() {
  qA.set('');
  [gradeA, typeA, gtypeA, clsA, attrA].forEach((a) => a.set([]));
  [lvlMinA, lvlMaxA, goldMinA, goldMaxA, priceMinA, priceMaxA].forEach((a) => a.set(''));
  [onlyTradA, onlyBookA, onlyFavA].forEach((a) => a.set(false));
}

// ---- sincroniza o estado na URL (replaceState) -------------------------------------
function syncUrl() {
  if (typeof location === 'undefined') return;
  const p = new URLSearchParams();
  const set = (k: string, v: string) => v && p.set(k, v);
  const setArr = (k: string, v: string[]) => v.length && p.set(k, v.join(','));
  set('q', qA.get());
  setArr('grade', gradeA.get());
  setArr('type', typeA.get());
  setArr('gt', gtypeA.get());
  setArr('cls', clsA.get());
  setArr('attr', attrA.get());
  set('lmin', lvlMinA.get());
  set('lmax', lvlMaxA.get());
  set('gmin', goldMinA.get());
  set('gmax', goldMaxA.get());
  set('pmin', priceMinA.get());
  set('pmax', priceMaxA.get());
  if (onlyTradA.get()) p.set('trad', '1');
  if (onlyBookA.get()) p.set('book', '1');
  if (onlyFavA.get()) p.set('fav', '1');
  if (curA.get() !== 'brl') p.set('cur', curA.get());
  if (sortKeyA.get() !== 'goldPer') p.set('sort', sortKeyA.get());
  if (sortDirA.get() !== 'desc') p.set('dir', sortDirA.get());
  if (viewA.get() !== 'cards') p.set('view', viewA.get());
  const qs = p.toString();
  history.replaceState(null, '', qs ? `?${qs}` : location.pathname);
}

// liga a sincronização (cada atom relevante dispara o syncUrl)
let wired = false;
export function wireUrlSync() {
  if (wired || typeof location === 'undefined') return;
  wired = true;
  [
    qA, gradeA, typeA, gtypeA, clsA, attrA, lvlMinA, lvlMaxA, goldMinA, goldMaxA,
    priceMinA, priceMaxA, onlyTradA, onlyBookA, onlyFavA, curA, sortKeyA, sortDirA, viewA,
  ].forEach((a) => a.subscribe(() => syncUrl()));
}
