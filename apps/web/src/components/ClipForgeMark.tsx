import type { SVGProps } from 'react';

/**
 * ClipForge's signal mark: a playhead at the centre of a clean broadcast wave.
 * It is hand-authored SVG, inherits its colour, and remains crisp from favicon
 * size through large marketing treatments.
 */
export default function ClipForgeMark({ className, ...props }: SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      aria-hidden="true"
      {...props}
    >
      <path
        d="M10.1 8.65a.9.9 0 0 1 1.36-.77l5.06 3.35a.92.92 0 0 1 0 1.54l-5.06 3.35a.9.9 0 0 1-1.36-.77v-6.7Z"
        fill="currentColor"
      />
      <path d="M7.2 7.3a6.25 6.25 0 0 0 0 9.4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      <path d="M4.3 4.5a10.15 10.15 0 0 0 0 15" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" opacity=".72" />
      <path d="M19.35 7.15a6.3 6.3 0 0 1 0 9.7" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" opacity=".86" />
    </svg>
  );
}
