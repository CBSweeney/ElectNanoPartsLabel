"""
Elect Nano Product Label Generator

This Streamlit application generates product labels with GS1 DataMatrix barcodes
overlaid on a PDF template. Users can upload a custom background template or
use the default provided template bundled with the app.

The GS1 barcode data is constructed according to GS1 application identifiers,
and the barcode is rendered using the treepoem library, ensuring correct
encoding of FNC1 and group separator characters.

Requirements:
- streamlit
- reportlab
- PyPDF2
- treepoem
- Pillow
- python-dateutil

Ghostscript must be installed on the system for treepoem to function correctly.
"""

from __future__ import annotations

import re
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Optional

import streamlit as st
from PyPDF2 import PdfReader, PdfWriter
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from streamlit_pdf_viewer import pdf_viewer

try:
    import treepoem  # type: ignore[import]
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "The 'treepoem' package is required to generate GS1 DataMatrix barcodes. "
        "Please add treepoem to your requirements and ensure Ghostscript is installed."
    ) from exc


def sanitize_lot_code(lot_code: str) -> str:
    """
    Sanitize the lot code string by removing characters not allowed in GS1 AI 10.

    GS1 CSET 82 rules for AI 10 allow uppercase letters, digits, hyphen, period, and slash.

    Parameters
    ----------
    lot_code : str
        The raw lot code input.

    Returns
    -------
    str
        Sanitized lot code containing only valid characters.
    """
    return re.sub(r'[^A-Z0-9\-\.\/]', '', lot_code.upper())


def sanitize_gs1_text(text: str) -> str:
    """
    Sanitize text for GS1 fields by uppercasing and removing disallowed characters.

    Allowed characters include uppercase letters, digits, hyphen, period, slash, and space.
    This matches typical GS1 encoding character sets for textual fields.

    Parameters
    ----------
    text : str
        Raw input text.

    Returns
    -------
    str
        Sanitized text suitable for GS1 encoding.
    """
    return re.sub(r'[^A-Z0-9\-\.\/ ]', '', text.upper())


def sanitize_ai91_text(text: str) -> str:
    """
    Sanitize text specifically for GS1 AI 91 (company internal information).

    Allowed characters are uppercase letters, digits, hyphen, period, and slash.
    The result is truncated to 40 characters as per GS1 AI 91 length restrictions.

    Parameters
    ----------
    text : str
        Raw input text.

    Returns
    -------
    str
        Sanitized and truncated text suitable for GS1 AI 91.
    """
    sanitized = re.sub(r'[^A-Z0-9\-\.\/]', '', text.upper())
    return sanitized[:40]


def build_gs1_string(
    gtin14: str,
    lot: str,
    mfg: date,
    country_code: str,
    sku: str,
    cust_po: str,
    cust_part: str,
    revision: str,
    note: str,
    quantity: int,
) -> str:
    """
    Assemble a GS1 application identifier string for encoding in the DataMatrix barcode.

    Each field is prefixed with its GS1 application identifier (AI) code.

    Parameters
    ----------
    gtin14 : str
        14-digit Global Trade Item Number.
    lot : str
        Lot or batch number (AI 10).
    mfg : date
        Manufacturing date (AI 11), encoded as YYMMDD.
    country_code : str
        Three-digit country code of origin (AI 422).
    sku : str
        SKU (AI 21).
    cust_po : str
        Customer PO Number (AI 400).
    cust_part : str
        Customer Part Number (AI 7021).
    revision : str
        Revision number (AI 7022).
    note : str
        Note (AI 91).
    quantity : int
        Quantity (AI 30), zero-padded to 5 digits.

    Returns
    -------
    str
        Concatenated GS1 data string ready for barcode encoding.
    """
    mfg_str = mfg.strftime("%y%m%d")
    sku_sanitized = sanitize_gs1_text(sku)
    cust_po_sanitized = sanitize_gs1_text(cust_po)
    cust_part_sanitized = sanitize_gs1_text(cust_part)
    revision_sanitized = sanitize_gs1_text(revision)
    qty_str = f"{quantity:05d}"
    return (
        f"(01){gtin14}"
        f"(30){qty_str}"
        f"(11){mfg_str}"
        f"(422){country_code}"
        f"(10){lot}"
        f"(21){sku_sanitized}"
        f"(400){cust_po_sanitized}"
        f"(7021){cust_part_sanitized}"
        f"(7022){revision_sanitized}"
        f"(91){note}"
    )


def generate_barcode_image(gs1_data: str) -> "PIL.Image.Image":
    """
    Generate a GS1 DataMatrix barcode as a PIL Image.

    Uses the treepoem library to render the barcode with correct GS1 formatting,
    automatically inserting FNC1 and group separator characters.

    Parameters
    ----------
    gs1_data : str
        The GS1 data string to encode.

    Returns
    -------
    PIL.Image.Image
        The generated barcode image in RGB mode.
    """
    barcode = treepoem.generate_barcode(
        barcode_type="gs1datamatrix",
        data=gs1_data,
        options={"parsefnc": True},
    )
    return barcode.convert("RGB")


@st.cache_data(show_spinner=False)
def render_label(
    sku: str,
    lot_code: str,
    mfg_date: date,
    coo_display: str,
    background_bytes: bytes,
    gs1_data: str,
    cust_part: str,
    revision: str,
    cust_po: str,
    note: str,
    quantity: int,
) -> bytes:
    """
    Render the final product label PDF by overlaying text and barcode onto the background template.

    Coordinates are specified in millimeters and converted to points for ReportLab.
    The overlay PDF is merged with the background template PDF to produce the final output.

    Parameters
    ----------
    sku : str
        Resin SKU.
    lot_code : str
        Sanitized lot code.
    mfg_date : date
        Manufacturing date.
    coo_display : str
        Country of origin display name.
    background_bytes : bytes
        Raw bytes of the background PDF template.
    gs1_data : str
        GS1 encoded data string for barcode.
    cust_part : str
        Customer part number.
    revision : str
        Revision number.
    cust_po : str
        Customer PO number.
    note : str
        Note text.
    quantity : int
        Quantity value.

    Returns
    -------
    bytes
        The rendered label as PDF bytes.
    """
    buffer = BytesIO()
    overlay_packet = BytesIO()

    # Define label size in points (mm converted)
    page_width = 152.5 * mm
    page_height = 101.6 * mm

    # Create a canvas for the overlay layer
    c = canvas.Canvas(overlay_packet, pagesize=(page_width, page_height))

    # Draw fixed text fields on the label at specified positions
    start_x = 5 * mm
    start_y = 73 * mm
    step_y = 5.75 * mm
    font_size = 14
    c.setFont("Helvetica", font_size)
    c.drawString(start_x, start_y, f"PART #: {cust_part}")
    c.drawString(start_x, start_y - step_y, f"REV #: {revision}")
    c.drawString(start_x, start_y - 2 * step_y, f"PO#: {cust_po}")
    c.drawString(start_x, start_y - 3 * step_y, f"NOTE: {note[:40]}")
    c.drawString(start_x, start_y - 4 * step_y, f"QUANTITY: {quantity}")
    c.drawString(start_x, start_y - 5 * step_y, f"RESIN SKU: {sku}")
    c.drawString(start_x, start_y - 6 * step_y, f"RESIN LOT #: {lot_code}")
    c.drawString(start_x, start_y - 7 * step_y, f"MFG. DATE: {mfg_date:%Y-%m-%d}")
    c.drawString(start_x, start_y - 8 * step_y, f"COO: {coo_display}")

    # Generate the DataMatrix barcode image and embed it
    barcode_img = generate_barcode_image(gs1_data)
    barcode_reader = ImageReader(barcode_img)
    # Position barcode at bottom-left corner with size 25mm x 25mm
    c.drawImage(
        barcode_reader,
        x=121.5 * mm,
        y=5.7 * mm,
        width=25 * mm,
        height=25 * mm,
    )
    c.save()

    # Merge the overlay PDF with the background template PDF
    overlay_packet.seek(0)
    overlay_pdf = PdfReader(overlay_packet)
    base_pdf = PdfReader(BytesIO(background_bytes))
    output = PdfWriter()
    page = base_pdf.pages[0]
    page.merge_page(overlay_pdf.pages[0])
    output.add_page(page)
    output.write(buffer)

    return buffer.getvalue()


def load_template(default_name: str = "Elect Nano 2025 Label Template V1.pdf") -> Optional[bytes]:
    """
    Load the PDF template from an uploaded file or fallback to a local bundled file.

    Checks if a template has been uploaded and stored in session state, returning its bytes.
    Otherwise attempts to read the default template file from the script's directory.

    Parameters
    ----------
    default_name : str
        Filename of the default template PDF.

    Returns
    -------
    Optional[bytes]
        The raw bytes of the template PDF if found, otherwise None.
    """
    uploaded = st.session_state.get("_uploaded_template")
    if uploaded is not None:
        return uploaded.getvalue()
    # Use the directory containing this script for fallback template
    script_dir = Path(__file__).resolve().parent
    candidate = script_dir / default_name
    if candidate.exists():
        return candidate.read_bytes()
    return None


def main() -> None:
    """
    Main Streamlit app function to gather user input, generate the label, and display/download it.
    """
    st.set_page_config(page_title="Product Label Generator", layout="centered")
    st.title("Elect Nano Product Label Generator from PDF Template")

    # Allow user to upload a custom PDF template; stored in session state for reuse
    with st.expander("⚙️ Template settings", expanded=False):
        uploaded_file = st.file_uploader(
            "Optional: upload a custom background PDF (one page only)",
            type=["pdf"],
            key="template_file",
            help=(
                "If no file is uploaded, the app will look for a bundled template named "
                "'Elect Nano 2025 Label Template V1.pdf' in the script directory."
            ),
        )
        if uploaded_file:
            st.session_state["_uploaded_template"] = uploaded_file
            st.success("Custom template uploaded successfully.")

    # Map country names to GS1 numeric codes
    country_options = {
        "United States": "840",
        "Japan": "392",
        "China": "156",
    }

    # Form for label input parameters
    with st.form("label_form"):
        sku = st.text_input("Resin SKU", value="X-MC-CO-EMI-000-01")
        lot_num = st.text_input("Resin Lot #", value="EN-YYMM-XX")

        # Sanitize lot number to comply with GS1 AI 10 rules
        raw_lot_num = lot_num
        lot_num = sanitize_lot_code(raw_lot_num)
        if lot_num != raw_lot_num:
            st.warning(
                "Lot # contains invalid characters and has been sanitized to comply with GS1 rules:\n"
                f"`{lot_num}`"
            )

        mfg_date = st.date_input("Manufacturing Date", value=date.today())
        quantity = st.number_input("Quantity", min_value=1, value=1)
        coo_display = st.selectbox("Country of Origin", options=list(country_options.keys()), index=0)
        coo_code = country_options[coo_display]

        cust_po_raw = st.text_input("Customer PO Number", value="123456789")
        cust_po = sanitize_gs1_text(cust_po_raw)

        cust_part_raw = st.text_input("Customer Part Number", value="123456789")
        cust_part = sanitize_gs1_text(cust_part_raw)

        revision_raw = st.text_input("Customer Part Revision Number", value="01")
        revision = sanitize_gs1_text(revision_raw)

        note_raw = st.text_input("Note", value="Enter Note Here")
        note = sanitize_ai91_text(note_raw)
        if note != note_raw:
            st.warning(
                "Note field contains invalid characters or is too long for GS1 AI 91 encoding. "
                f"It will be encoded as:\n`{note}`"
            )

        submitted = st.form_submit_button("Generate Label")

        # Prevent label generation if lot number contains invalid characters
        if submitted and lot_num != raw_lot_num:
            st.error(
                "Label generation stopped: Lot # contains invalid characters "
                "that are not allowed in GS1 DataMatrix (AI 10)."
            )
            return

    # Proceed only after form submission
    if submitted:
        bg_bytes = load_template()
        if bg_bytes is None:
            st.error(
                "No background PDF found. Please upload a template or place the default file "
                "named 'Elect Nano 2025 Label Template V1.pdf' in the script directory."
            )
            return

        # Build GS1 data string for barcode encoding
        gs1_data = build_gs1_string(
            gtin14="00069766967842",  # Example GTIN14 with USA prefix and check digit
            lot=lot_num,
            mfg=mfg_date,
            country_code=coo_code,
            sku=sku,
            cust_po=cust_po,
            cust_part=cust_part,
            revision=revision,
            note=note,
            quantity=quantity,
        )
        st.code(gs1_data, language="text")

        # Render the label PDF with overlay and barcode
        pdf_bytes = render_label(
            sku=sku,
            lot_code=lot_num,
            mfg_date=mfg_date,
            coo_display=coo_display,
            background_bytes=bg_bytes,
            gs1_data=gs1_data,
            cust_part=cust_part,
            revision=revision,
            cust_po=cust_po,
            note=note_raw,
            quantity=quantity,
        )

        st.success("✅ Label generated successfully!")

        # Provide download button for the generated PDF label
        st.download_button(
            label="📥 Download Label PDF",
            data=pdf_bytes,
            file_name=f"{cust_po}_{cust_part}.pdf",
            mime="application/pdf",
        )

        # Display the generated PDF in an interactive viewer
        pdf_viewer(pdf_bytes, width=700, height=1000, zoom_level=1.0)


if __name__ == "__main__":  # pragma: no cover
    main()
