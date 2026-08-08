import { FC } from 'react'

// The footer previously carried an upstream credit line and nothing else.
// Rendering null keeps the export in place for its two call sites without
// leaving an empty padded strip under the page.
export const Footer: FC = () => null
