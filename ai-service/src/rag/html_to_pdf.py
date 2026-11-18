"""Convert the saved gem HTML pages into searchable PDFs."""

import argparse
import os
from pathlib import Path

import pymupdf
from playwright.sync_api import Browser, sync_playwright


AI_SERVICE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_INPUT_DIR = AI_SERVICE_DIR / "rag_documents" / "gems"
DEFAULT_OUTPUT_DIR = AI_SERVICE_DIR / "rag_documents" / "gems_pdfs"


def find_browser(requested_path: Path | None = None) -> Path:
    """Return the requested browser or an installed Edge/Chrome executable."""
    if requested_path:
        browser_path = requested_path.expanduser().resolve()
        if browser_path.is_file():
            return browser_path
        raise FileNotFoundError(f"Browser executable not found: {browser_path}")

    candidates = [
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ]
    for browser in candidates:
        if browser.is_file():
            return browser

    raise FileNotFoundError(
        "Microsoft Edge or Google Chrome was not found. "
        "Install one, or pass its executable with --browser."
    )


def optimize_pdf(pdf_path: Path) -> None:
    """Reduce image size while preserving selectable text and image objects."""
    optimized_path = pdf_path.with_suffix(".optimized.pdf")
    try:
        with pymupdf.open(pdf_path) as document:
            document.rewrite_images(
                dpi_threshold=180,
                dpi_target=144,
                quality=75,
            )
            document.save(
                optimized_path,
                garbage=4,
                clean=True,
                deflate=True,
                deflate_images=True,
                deflate_fonts=True,
            )
        os.replace(optimized_path, pdf_path)
    finally:
        optimized_path.unlink(missing_ok=True)


def load_all_images(page) -> None:
    """Trigger images that the saved webpage marked for lazy loading."""
    page.evaluate(
        """
        async () => {
            document.querySelectorAll("img").forEach((image) => {
                image.loading = "eager";
            });
            const step = Math.max(window.innerHeight, 800);
            for (
                let position = 0;
                position < document.documentElement.scrollHeight;
                position += step
            ) {
                window.scrollTo(0, position);
                await new Promise((resolve) => setTimeout(resolve, 20));
            }
            window.scrollTo(0, 0);
            await new Promise((resolve) => setTimeout(resolve, 1000));
        }
        """
    )


def convert_html_to_pdf(
    browser: Browser,
    html_path: Path,
    output_dir: Path,
    overwrite: bool = False,
    optimize: bool = True,
) -> tuple[Path, bool]:
    """Print one saved HTML page to PDF and return (path, was_created)."""
    html_path = html_path.resolve()
    output_path = output_dir / f"{html_path.stem}.pdf"
    temporary_path = output_dir / f".{html_path.stem}.temporary.pdf"

    if output_path.exists() and not overwrite:
        print(f"Skipping existing: {output_path.name}")
        return output_path, False

    page = browser.new_page()
    try:
        page.goto(html_path.as_uri(), wait_until="load", timeout=120_000)
        load_all_images(page)
        page.pdf(
            path=str(temporary_path),
            format="A4",
            print_background=True,
            display_header_footer=False,
            prefer_css_page_size=True,
        )
        if optimize:
            optimize_pdf(temporary_path)
        os.replace(temporary_path, output_path)
        print(f"Created: {output_path}")
        return output_path, True
    finally:
        page.close()
        temporary_path.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert saved gem HTML pages to searchable PDFs."
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"HTML folder (default: {DEFAULT_INPUT_DIR})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"PDF folder (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument("--file", help="Convert only one HTML filename.")
    parser.add_argument(
        "--browser",
        type=Path,
        help="Optional path to an Edge or Chrome executable.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace previously generated PDFs.",
    )
    parser.add_argument(
        "--no-optimize",
        action="store_true",
        help="Skip the PDF image-compression pass.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_dir = args.input_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()

    if not input_dir.is_dir():
        raise NotADirectoryError(f"Input directory not found: {input_dir}")

    if args.file:
        requested_file = Path(args.file)
        if requested_file.name != args.file:
            raise ValueError("--file must be a filename, not a path")
        html_files = [input_dir / requested_file]
    else:
        html_files = sorted(input_dir.glob("*.html"))

    if not html_files:
        raise FileNotFoundError(f"No HTML files found in: {input_dir}")
    missing_files = [path for path in html_files if not path.is_file()]
    if missing_files:
        raise FileNotFoundError(f"HTML file not found: {missing_files[0]}")

    output_dir.mkdir(parents=True, exist_ok=True)
    browser_path = find_browser(args.browser)
    created = 0
    skipped = 0
    failures: list[tuple[Path, Exception]] = []

    print(f"Input:   {input_dir}")
    print(f"Output:  {output_dir}")
    print(f"Browser: {browser_path}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=str(browser_path),
            args=["--allow-file-access-from-files"],
        )
        try:
            for html_file in html_files:
                try:
                    _, was_created = convert_html_to_pdf(
                        browser,
                        html_file,
                        output_dir,
                        overwrite=args.overwrite,
                        optimize=not args.no_optimize,
                    )
                    created += int(was_created)
                    skipped += int(not was_created)
                except Exception as error:  # Keep processing the rest of the batch.
                    failures.append((html_file, error))
                    print(f"Failed: {html_file.name}: {error}")
        finally:
            browser.close()

    print(
        f"Finished: {created} created, {skipped} skipped, "
        f"{len(failures)} failed."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
