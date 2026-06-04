import {
  ChevronDown,
  ChevronRight,
  Check,
  Copy,
  SquarePen,
  Keyboard,
  X,
  Maximize2,
  Play,
  ArrowUp,
  ArrowDown,
  ArrowDownToLine,
  type LucideProps,
} from "lucide-react";
import type { ComponentType } from "react";

// Single source of truth for iconography. Call sites use semantic names and
// never import lucide-react (or raw <svg>) directly, so the backing library can
// be swapped behind this layer without touching usages.
export type IconName =
  | "chevron-down"
  | "chevron-right"
  | "check"
  | "copy"
  | "edit"
  | "keyboard"
  | "close"
  | "fullscreen"
  | "play"
  | "scroll-up"
  | "scroll-down"
  | "scroll-bottom";

const REGISTRY: Record<IconName, ComponentType<LucideProps>> = {
  "chevron-down": ChevronDown,
  "chevron-right": ChevronRight,
  check: Check,
  copy: Copy,
  edit: SquarePen,
  keyboard: Keyboard,
  close: X,
  fullscreen: Maximize2,
  play: Play,
  "scroll-up": ArrowUp,
  "scroll-down": ArrowDown,
  "scroll-bottom": ArrowDownToLine,
};

export interface IconProps extends Omit<LucideProps, "ref"> {
  name: IconName;
  size?: number;
}

// Decorative by default (aria-hidden): every current usage sits inside a button
// or control that already carries its own title/aria-label. Pass aria-hidden={false}
// + aria-label to override for a standalone meaningful icon.
export function Icon({ name, size = 16, className, ...rest }: IconProps) {
  const Cmp = REGISTRY[name];
  return <Cmp size={size} aria-hidden className={className ? `icon ${className}` : "icon"} {...rest} />;
}
