import { Crosshair, Crown, Shield, Swords } from "lucide-react";

const marks = {
  CS2: Crosshair,
  "Dota 2": Swords,
  Valorant: Shield,
  "League of Legends": Crown,
};

export function SportMark({ sport, size = 18 }) {
  const Icon = marks[sport] ?? Crosshair;
  return <Icon aria-hidden="true" size={size} strokeWidth={1.8} />;
}
