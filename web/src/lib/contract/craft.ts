export interface CraftRecipe {
    type:     Type;
    tier:     number;
    lvl:      number[];
    distinct: number;
    mats:     Best[];
    cost:     number | null;
    odds:     Odd[];
    grades:   GradeElement[];
    floor:    number;
    ceil:     number;
    ev:       number | null;
    best:     Best;
    pWin:     number | null;
    verdict:  Verdict;
}

export interface Best {
    grade?: GradeEnum;
    name:   string;
    icon:   string;
    mname?: string;
    price:  number | null;
    count?: number;
}

export type GradeEnum = "BEYOND" | "ARCANA" | "IMMORTAL" | "LEGENDARY" | "UNCOMMON" | "RARE" | "COMMON";

export interface GradeElement {
    grade: GradeEnum;
    pct:   number;
    n:     number;
    ntot:  number;
    floor: number | null;
    ceil:  number | null;
    best:  Best | null;
}

export interface Odd {
    grade: GradeEnum;
    pct:   number;
}

export type Type = "Accessory" | "Helmet" | "Gloves" | "MainWeapon" | "Armor" | "SubWeapon" | "Boots";

export type Verdict = "craft" | "gamble" | "unknown";
