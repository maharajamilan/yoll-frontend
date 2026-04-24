import type { Codebook, WaveData } from "./types";

export async function loadCodebook(wave: string): Promise<Codebook> {
  const res = await fetch(`/data/codebook_${wave.toLowerCase()}.json`);
  if (!res.ok) throw new Error(`Failed to load codebook for ${wave}`);
  return res.json();
}

export async function loadWaveData(wave: string): Promise<WaveData> {
  const res = await fetch(`/data/data_${wave.toLowerCase()}.json`);
  if (!res.ok) throw new Error(`Failed to load data for ${wave}`);
  return res.json();
}
