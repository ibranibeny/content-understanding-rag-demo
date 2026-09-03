import { render, screen } from "@testing-library/react";

import { App } from "./App";

test("renders the workshop identity and safety notice", () => {
  render(<App />);

  expect(
    screen.getByRole("heading", { name: /document intelligence console/i }),
  ).toBeVisible();
  expect(screen.getByText(/do not upload confidential information/i)).toBeVisible();
});
