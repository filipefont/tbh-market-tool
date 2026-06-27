export interface Stage {
    label:      string;
    act:        number;
    no:         number;
    level:      number;
    name:       string;
    type:       Type;
    difficulty: Difficulty;
    boss:       string;
    ev:         number;
    top:        Array<Array<number | string>>;
    drops:      Drop[];
}

export type Difficulty = "NORMAL" | "NIGHTMARE" | "HELL" | "TORMENT";

export interface Drop {
    name:   string;
    icon:   Icon;
    grade:  Grade;
    rate:   number | null;
    source: Source;
}

export type Grade = "COMMON" | "RARE" | "LEGENDARY" | "IMMORTAL" | "ARCANA" | "BEYOND" | "CELESTIAL";

export type Icon = "Item_910011" | "Item_920011" | "Item_930011" | "Item_190001" | "Item_190002" | "Item_190003" | "Item_190004";

export type Source = "monster" | "boss" | "soulstone";

export type Type = "NORMAL" | "ACTBOSS";
