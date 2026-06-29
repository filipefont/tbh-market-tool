import type { Currency } from '../format.ts';
import {
  attrA, clsA, curA, favsA, goldMaxA, goldMinA, gradeA, gtypeA, lvlMaxA, lvlMinA,
  onlyBookA, onlyFavA, onlyTradA, priceMaxA, priceMinA, qA, sortDirA, sortKeyA, typeA, viewA,
} from './market.ts';

// Espelho reativo (runes) dos atoms nanostores, p/ os componentes Svelte lerem.
// Módulo singleton: filtros (sidebar) e tabela (main) compartilham o mesmo estado.
export const ms = $state({
  q: qA.get(),
  grade: [...gradeA.get()],
  type: [...typeA.get()],
  gtype: [...gtypeA.get()],
  cls: [...clsA.get()],
  attr: [...attrA.get()],
  lvlMin: lvlMinA.get(),
  lvlMax: lvlMaxA.get(),
  goldMin: goldMinA.get(),
  goldMax: goldMaxA.get(),
  priceMin: priceMinA.get(),
  priceMax: priceMaxA.get(),
  onlyTrad: onlyTradA.get(),
  onlyBook: onlyBookA.get(),
  onlyFav: onlyFavA.get(),
  cur: curA.get() as Currency,
  sortKey: sortKeyA.get(),
  sortDir: sortDirA.get(),
  view: viewA.get(),
  favs: [...favsA.get()],
});

qA.subscribe((v) => (ms.q = v));
gradeA.subscribe((v) => (ms.grade = [...v]));
typeA.subscribe((v) => (ms.type = [...v]));
gtypeA.subscribe((v) => (ms.gtype = [...v]));
clsA.subscribe((v) => (ms.cls = [...v]));
attrA.subscribe((v) => (ms.attr = [...v]));
lvlMinA.subscribe((v) => (ms.lvlMin = v));
lvlMaxA.subscribe((v) => (ms.lvlMax = v));
goldMinA.subscribe((v) => (ms.goldMin = v));
goldMaxA.subscribe((v) => (ms.goldMax = v));
priceMinA.subscribe((v) => (ms.priceMin = v));
priceMaxA.subscribe((v) => (ms.priceMax = v));
onlyTradA.subscribe((v) => (ms.onlyTrad = v));
onlyBookA.subscribe((v) => (ms.onlyBook = v));
onlyFavA.subscribe((v) => (ms.onlyFav = v));
curA.subscribe((v) => (ms.cur = v));
sortKeyA.subscribe((v) => (ms.sortKey = v));
sortDirA.subscribe((v) => (ms.sortDir = v));
viewA.subscribe((v) => (ms.view = v));
favsA.subscribe((v) => (ms.favs = [...v]));
