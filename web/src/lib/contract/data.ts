export interface MarketItem {
    name:       string;
    key:        number;
    base:       string;
    type:       Type;
    grade:      Grade;
    gradeRank:  number;
    icon:       string;
    level:      number | null;
    gold:       number;
    usd:        number | null;
    listings:   number;
    real:       Real | null;
    book:       Book | null;
    gearType:   GearType | null;
    gearGroup:  GearGroup | null;
    parts:      GearType | null;
    classes:    Class[];
    variant:    Variant | null;
    tradable:   boolean;
    slots?:     Slots;
    uniqueMod?: string;
    attrs?:     { [key: string]: Attr };
    chg24?:     number;
    chg7?:      number;
    gradeLock?: boolean;
    droppedIn?: DroppedIn[];
    effects?:   Effect[];
    noBulk?:    boolean;
}

export interface Attr {
    value: number;
    disp:  string;
}

export interface Book {
    brl: BookBrl;
}

export interface BookBrl {
    buyMax:      number | null;
    buyOrders:   number;
    sellMin:     number | null;
    sellOrders:  number;
    buyBook:     Array<number[]>;
    buyNotional: number;
    fetchedAt:   number;
}

export type Class = "All" | "Hunter" | "Knight" | "Priest" | "Sorcerer" | "Slayer" | "Ranger";

export interface DroppedIn {
    stage:  Stage;
    level:  number;
    rate:   null;
    source: Source;
}

export type Source = "soulstone";

export type Stage = "1-10" | "2-10" | "3-10";

export interface Effect {
    slot:   Slot;
    stat:   string;
    disp:   string;
    chance: number;
}

export type Slot = "Weapon" | "Armor" | "Accessory" | "All";

export type GearGroup = "ARMOR" | "WEAPON" | "ACCESSORY";

export type GearType = "HELMET" | "CROSSBOW" | "ARMOR" | "SWORD" | "EARING" | "AMULET" | "RING" | "TOME" | "BRACER" | "BOOTS" | "SHIELD" | "ORB" | "HATCHET" | "BOW" | "SCEPTER" | "STAFF" | "GLOVES" | "AXE" | "ARROW" | "BOLT" | "MAIN_WEAPON" | "SUB_WEAPON";

export type Grade = "ARCANA" | "BEYOND" | "LEGENDARY" | "CELESTIAL" | "COSMIC" | "DIVINE" | "IMMORTAL" | "RARE" | "UNCOMMON" | "COMMON";

export interface Real {
    brl: RealBrl;
}

export interface RealBrl {
    low:        number | null;
    lowText:    null | string;
    med:        number | null;
    medText:    null | string;
    vol:        number | null;
    fetchedAt?: number;
}

export interface Slots {
    decoration:  number;
    engraving:   number;
    inscription: number;
}

export type Type = "GEAR" | "MATERIAL";

export type Variant = "A";
