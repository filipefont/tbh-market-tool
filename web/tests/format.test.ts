import { describe, expect, it } from 'vitest';
import { fmtAbbr, fmtDelta, fmtPrice, trendOf } from '../src/lib/format.ts';
import { gradeColor, GRADE_COLORS } from '../src/lib/grades.ts';

describe('format', () => {
  it('fmtAbbr abrevia milhares/milhões', () => {
    expect(fmtAbbr(950)).toBe('950');
    expect(fmtAbbr(1234)).toBe('1.2k');
    expect(fmtAbbr(3_400_000)).toBe('3.4M');
    expect(fmtAbbr(null)).toBe('—');
  });

  it('fmtPrice usa o símbolo da moeda', () => {
    expect(fmtPrice(12.3, 'brl')).toBe('R$ 12.30');
    expect(fmtPrice(5, 'usd')).toBe('$ 5.00');
    expect(fmtPrice(null, 'brl')).toBe('—');
  });

  it('trendOf classifica a variação', () => {
    expect(trendOf(2)).toBe('up');
    expect(trendOf(-2)).toBe('down');
    expect(trendOf(0)).toBe('flat');
    expect(trendOf(null)).toBe('flat');
  });

  it('fmtDelta formata com sinal', () => {
    expect(fmtDelta(3.21)).toBe('+3.2%');
    expect(fmtDelta(-1)).toBe('-1.0%');
  });
});

describe('grades', () => {
  it('tem cor p/ toda grade e fallback', () => {
    expect(GRADE_COLORS.ARCANA).toBe('#a855f7');
    expect(gradeColor('ARCANA')).toBe('#a855f7');
    expect(gradeColor('DESCONHECIDA')).toBe('#9aa3b8');
    expect(gradeColor(null)).toBe('#9aa3b8');
  });
});
