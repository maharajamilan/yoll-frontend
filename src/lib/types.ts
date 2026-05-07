export type ResponseCode = number | string;

export type CodebookOption = {
  code: ResponseCode;
  label: string;
};

export type MaxDiffItem = {
  /** The numeric code that the main column carries when this item is picked. */
  code: ResponseCode;
  /** Human-readable item label (e.g. "Cost of living/affordability"). */
  label: string;
  /**
   * Name of the sibling `_do_N` column in the data file. Non-null on this
   * column iff the item was shown to that respondent.
   * Single-task MaxDiff: the value is just a slot indicator (1 or 2).
   * Multi-task MaxDiff: the value is an integer count of how many rounds
   * this respondent was shown the item.
   */
  do_col: string;
  /**
   * Multi-task MaxDiff only. Companion to `do_col`: integer count of how
   * many rounds this respondent picked this item. When absent (single-task
   * MaxDiff), wins are inferred by checking whether the main column's pick
   * matches `code`.
   */
  pick_col?: string;
};

export type CodebookColumn = {
  label: string;
  question: string;
  type: "categorical" | "numeric" | "maxdiff";
  /** For categoricals only. */
  options?: CodebookOption[];
  /** For MaxDiffs only. */
  items?: MaxDiffItem[];
  waves: string[];
};

export type WaveMeta = {
  label: string;
  n: number;
  note?: string;
};

export type Codebook = {
  waves: Record<string, WaveMeta>;
  columns: Record<string, CodebookColumn>;
};

export type WaveData = {
  wave: string;
  n: number;
  columns: string[];
  rows: (ResponseCode | null)[][];
  weights: number[];
};

export type Bucket = {
  id: string;
  name: string;
  codes: ResponseCode[];
};

/**
 * A single dimension inside a Group: one survey column and its ordered buckets.
 * A Group may have multiple dimensions; effective columns are the Cartesian
 * product of the dimensions' buckets.
 */
export type Dimension = {
  id: string;
  column: string | null;
  buckets: Bucket[];
};

export type Group = {
  id: string;
  dimensions: Dimension[];
};

/**
 * Each question may optionally override its row layout with custom buckets
 * (combine "Strongly approve" + "Somewhat approve" into "Approve", etc.).
 * When `responseBuckets` is null, the codebook's options are used as-is.
 */
export type Question = {
  id: string;
  column: string;
  responseBuckets: Bucket[] | null;
};

export type Config = {
  wave: string | null;
  includeTotal: boolean;
  groups: Group[];
  questions: Question[];
};
