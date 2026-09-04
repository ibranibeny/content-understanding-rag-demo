import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, test, vi } from "vitest";

import { DocumentUploader } from "./DocumentUploader";

function file(name: string, type: string, size = 4): File {
  return new File([new Uint8Array(size)], name, { type });
}

function renderUploader(overrides: Partial<React.ComponentProps<typeof DocumentUploader>> = {}) {
  const onUpload = vi.fn();
  const onError = vi.fn();
  const props = { busy: false, progress: null, onUpload, onError, ...overrides };
  const result = render(<DocumentUploader {...props} />);
  return { ...result, onUpload, onError };
}

function choose(selectedFile: File): void {
  fireEvent.change(screen.getByLabelText(/choose document file/i), { target: { files: [selectedFile] } });
}

describe("DocumentUploader", () => {
  test.each([
    ["report.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
    ["slides.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"],
    ["scan.png", "image/png"],
    ["photo.jpg", "image/jpeg"],
  ])("uploads accepted non-PDF %s immediately without page controls", (name, type) => {
    const { onUpload } = renderUploader();
    const selectedFile = file(name, type);

    choose(selectedFile);

    expect(onUpload).toHaveBeenCalledWith(selectedFile, undefined);
    expect(screen.queryByRole("group", { name: /page scope/i })).not.toBeInTheDocument();
  });

  test("retains the supported-type validation", () => {
    const { onUpload, onError } = renderUploader();

    choose(file("notes.txt", "text/plain"));

    expect(onError).toHaveBeenCalledWith("Choose a PDF, DOCX, PPTX, PNG, or JPEG file.");
    expect(onUpload).not.toHaveBeenCalled();
  });

  test("retains the 100 MB maximum", () => {
    const { onUpload, onError } = renderUploader();
    const oversized = file("large.pdf", "application/pdf");
    Object.defineProperty(oversized, "size", { value: 100 * 1024 * 1024 + 1 });

    choose(oversized);

    expect(onError).toHaveBeenCalledWith("Files must be 100 MB or smaller.");
    expect(onUpload).not.toHaveBeenCalled();
  });

  test("holds a PDF pending and exposes all semantic page scope modes", () => {
    const { onUpload } = renderUploader();

    choose(file("annual-report.pdf", "application/pdf"));

    expect(onUpload).not.toHaveBeenCalled();
    expect(screen.getByText("annual-report.pdf")).toBeVisible();
    expect(screen.getByRole("group", { name: /page scope/i })).toBeVisible();
    expect(screen.getByRole("radio", { name: /all pages/i })).toBeChecked();
    expect(screen.getByRole("radio", { name: /start \/ end/i })).toBeVisible();
    expect(screen.getByRole("radio", { name: /advanced/i })).toBeVisible();
    expect(screen.getByText(/pdfs over 300 pages must use a range/i)).toBeVisible();
  });

  test("uploads all pages only after explicit submission", () => {
    const { onUpload } = renderUploader();
    const selectedFile = file("short.pdf", "application/pdf");
    choose(selectedFile);

    expect(onUpload).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /upload and process/i }));

    expect(onUpload).toHaveBeenCalledWith(selectedFile, undefined);
  });

  test("clears the pending PDF after a successful submission", () => {
    renderUploader();
    choose(file("single-submit.pdf", "application/pdf"));

    fireEvent.click(screen.getByRole("button", { name: /upload and process/i }));

    expect(screen.queryByRole("group", { name: /page scope/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /choose document/i })).toBeVisible();
  });

  test("normalizes and uploads a simple finite page range", () => {
    const { onUpload } = renderUploader();
    const selectedFile = file("archive.pdf", "application/pdf");
    choose(selectedFile);
    fireEvent.click(screen.getByRole("radio", { name: /start \/ end/i }));

    fireEvent.change(screen.getByLabelText(/^start page$/i), { target: { value: "301" } });
    fireEvent.change(screen.getByLabelText(/^end page$/i), { target: { value: "600" } });
    fireEvent.click(screen.getByRole("button", { name: /upload and process/i }));

    expect(onUpload).toHaveBeenCalledWith(selectedFile, "301-600");
  });

  test("normalizes and uploads an advanced finite page range", () => {
    const { onUpload } = renderUploader();
    const selectedFile = file("extract.pdf", "application/pdf");
    choose(selectedFile);
    fireEvent.click(screen.getByRole("radio", { name: /advanced/i }));

    fireEvent.change(screen.getByLabelText(/pages and ranges/i), { target: { value: " 1 - 3, 5 " } });
    fireEvent.click(screen.getByRole("button", { name: /upload and process/i }));

    expect(onUpload).toHaveBeenCalledWith(selectedFile, "1-3,5");
  });

  test.each([
    ["simple", "missing or invalid start and end pages"],
    ["advanced", "more than 300 selected pages"],
  ])("shows an inline associated alert and does not upload for %s input", (mode) => {
    const { onUpload } = renderUploader();
    choose(file("invalid.pdf", "application/pdf"));

    if (mode === "simple") {
      fireEvent.click(screen.getByRole("radio", { name: /start \/ end/i }));
      fireEvent.change(screen.getByLabelText(/^start page$/i), { target: { value: "10" } });
      fireEvent.change(screen.getByLabelText(/^end page$/i), { target: { value: "1" } });
    } else {
      fireEvent.click(screen.getByRole("radio", { name: /advanced/i }));
      fireEvent.change(screen.getByLabelText(/pages and ranges/i), { target: { value: "1-301" } });
    }

    fireEvent.click(screen.getByRole("button", { name: /upload and process/i }));

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/valid finite range of up to 300 pages/i);
    const activeInput = mode === "simple" ? screen.getByLabelText(/^start page$/i) : screen.getByLabelText(/pages and ranges/i);
    expect(activeInput).toHaveAttribute("aria-describedby", expect.stringContaining(alert.id));
    expect(onUpload).not.toHaveBeenCalled();
  });

  test("choosing another file clears pending state, fields, errors, and the native input", () => {
    renderUploader();
    const selectedFile = file("repeat.pdf", "application/pdf");
    choose(selectedFile);
    fireEvent.click(screen.getByRole("radio", { name: /advanced/i }));
    fireEvent.change(screen.getByLabelText(/pages and ranges/i), { target: { value: "1-301" } });
    fireEvent.click(screen.getByRole("button", { name: /upload and process/i }));
    const input = screen.getByLabelText(/choose document file/i) as HTMLInputElement;

    fireEvent.click(screen.getByRole("button", { name: /choose another file/i }));

    expect(screen.queryByText("repeat.pdf")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(input.value).toBe("");
    choose(selectedFile);
    expect(screen.getByRole("radio", { name: /all pages/i })).toBeChecked();
    expect(screen.queryByDisplayValue("1-301")).not.toBeInTheDocument();
  });

  test("disables every file-changing, mode, range, and submit control while busy", () => {
    function Harness() {
      const [busy, setBusy] = useState(false);
      return <>
        <button type="button" onClick={() => setBusy(true)}>Set busy</button>
        <DocumentUploader busy={busy} progress={0} onUpload={vi.fn()} onError={vi.fn()} />
      </>;
    }
    render(<Harness />);
    choose(file("busy.pdf", "application/pdf"));
    fireEvent.click(screen.getByRole("radio", { name: /start \/ end/i }));
    fireEvent.click(screen.getByRole("button", { name: /set busy/i }));

    expect(screen.getByLabelText(/choose document file/i)).toBeDisabled();
    expect(screen.getByRole("button", { name: /choose another file/i })).toBeDisabled();
    for (const radio of screen.getAllByRole("radio")) expect(radio).toBeDisabled();
    expect(screen.getByLabelText(/^start page$/i)).toBeDisabled();
    expect(screen.getByLabelText(/^end page$/i)).toBeDisabled();
    expect(screen.getByRole("button", { name: /upload and process/i })).toBeDisabled();
  });
});