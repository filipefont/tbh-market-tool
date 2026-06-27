// Contrato de api/history.json — escrito à mão (mapa com chaves dinâmicas, que o
// quicktype não modela bem). Mapa de nome-do-item -> série temporal de preços.
// Cada ponto é uma tupla [timestamp Unix em segundos, preço].

/** Um ponto da série: `[unixSeconds, price]`. */
export type HistoryPoint = [number, number];

/** Série temporal de um item (ordenada por tempo crescente). */
export type HistorySeries = HistoryPoint[];

/** api/history.json — mapeia o nome do item para sua série de preços. */
export type History = Record<string, HistorySeries>;
