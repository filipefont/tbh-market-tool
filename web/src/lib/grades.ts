import type { Grade } from './contract/index.ts';

// Cores de raridade (portadas de GRADE_COLORS no build.py — fonte da verdade do
// legado). Tipadas pela union Grade do contrato: se o Python adicionar uma grade,
// o type-check exige a cor aqui.
export const GRADE_COLORS: Record<Grade, string> = {
  COMMON: '#9aa4b2',
  UNCOMMON: '#4ade80',
  RARE: '#38bdf8',
  LEGENDARY: '#f59e0b',
  IMMORTAL: '#ef4444',
  ARCANA: '#a855f7',
  BEYOND: '#ec4899',
  CELESTIAL: '#22d3ee',
  DIVINE: '#f2e7c4',
  COSMIC: '#e879f9',
};

const FALLBACK = '#9aa3b8';

/** Cor da grade (com fallback p/ grades desconhecidas). */
export function gradeColor(grade: string | null | undefined): string {
  return (grade && GRADE_COLORS[grade as Grade]) || FALLBACK;
}

/** Ordem de raridade (crescente) p/ ordenação/legenda. */
export const GRADE_ORDER: Grade[] = [
  'COMMON',
  'UNCOMMON',
  'RARE',
  'LEGENDARY',
  'IMMORTAL',
  'ARCANA',
  'BEYOND',
  'CELESTIAL',
  'DIVINE',
  'COSMIC',
];
