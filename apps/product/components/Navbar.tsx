import { ReactNode } from "react";

import ProductShell from "./ProductShell";

export default function Navbar({ children }: { children?: ReactNode }) {
  return <ProductShell>{children}</ProductShell>;
}
