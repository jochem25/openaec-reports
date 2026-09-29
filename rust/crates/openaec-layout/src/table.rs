//! Table flowable — grid layout with optional headers, styling, and page splitting.

use crate::draw::DrawList;
use crate::flowable::{Flowable, LayoutContext, SplitResult};
use crate::types::{Alignment, Color, Padding, Pt, Size};

/// Table cell content.
#[derive(Debug, Clone)]
pub enum CellContent {
    Text(String),
    Empty,
}

/// Table visual style.
#[derive(Debug, Clone)]
pub struct TableStyleConfig {
    pub header_background: Option<Color>,
    pub header_text_color: Color,
    pub grid_color: Color,
    /// Rasterlijnen. Bij `Pt(0.0)` of kleiner worden GEEN cellijnen getekend
    /// ("clean" rapportstijl); combineer met `header_rule` voor alleen een
    /// lijn onder de koprij.
    pub grid_width: Pt,
    pub row_backgrounds: Vec<Option<Color>>,
    pub cell_padding: Padding,
    pub font_name: String,
    pub header_font_name: String,
    pub font_size: Pt,
    pub header_font_size: Pt,
    /// Teken (ook zonder raster) een dunne lijn onder de koprij.
    pub header_rule: bool,
}

impl Default for TableStyleConfig {
    fn default() -> Self {
        Self {
            header_background: Some(Color::rgb(64, 18, 74)), // OpenAEC purple
            header_text_color: Color::WHITE,
            grid_color: Color::rgb(200, 200, 200),
            grid_width: Pt(0.5),
            row_backgrounds: vec![None, Some(Color::rgb(245, 245, 245))],
            cell_padding: Padding::new(Pt(3.0), Pt(4.0), Pt(3.0), Pt(4.0)),
            font_name: "LiberationSans".to_string(),
            header_font_name: "LiberationSans-Bold".to_string(),
            font_size: Pt(9.0),
            header_font_size: Pt(9.0),
            header_rule: false,
        }
    }
}

/// Optionele stijl-afwijking per datarij (index = datarij-index, header telt
/// niet mee). Gebruikt voor hoofdstuk- (vet), paragraaf- (vet-cursief),
/// opmerking- (cursief) en subtotaalregels in de "clean" rapportstijl.
#[derive(Debug, Clone, Default)]
pub struct RowOverride {
    /// Ander lettertype voor de hele rij (bv. "LiberationSans-Bold").
    pub font_name: Option<String>,
    /// Dunne lijn boven de rij (bv. hoofdstukregels in besteksopmaak).
    pub top_rule: bool,
    /// Dunne lijn onder de rij.
    pub bottom_rule: bool,
    /// Som-lijn boven de rij (subtotalen): zwart en dikker, loopt ~5 mm
    /// rechts voorbij de tabel door, met een '+'-teken boven het uiteinde.
    pub top_rule_sum: bool,
}

/// Geschatte breedte van één teken, uitgedrukt in em (× font_size).
///
/// Genoeg om terugloop op te baseren zonder de fonttabellen in te lezen. De
/// waarden liggen dicht bij Liberation Sans / DejaVu Sans: kapitalen en
/// cijfers zijn duidelijk breder dan de oude vlakke schatting van 0.5, smalle
/// letters juist smaller. Zonder dit onderscheid liep een titel in kapitalen
/// over de volgende kolom heen.
fn char_em_width(c: char) -> f32 {
    match c {
        ' ' => 0.28,
        'i' | 'j' | 'l' | 'I' | '.' | ',' | ':' | ';' | '\'' | '|' | '!' | '`' => 0.26,
        'f' | 't' | 'r' | '(' | ')' | '[' | ']' | '-' | '/' | '\\' => 0.34,
        'm' | 'w' => 0.79,
        'M' | 'W' => 0.87,
        c if c.is_ascii_uppercase() => 0.67,
        c if c.is_ascii_digit() => 0.56,
        c if c.is_ascii_lowercase() => 0.53,
        // Accenten, €, ², ³ en overige tekens: net iets ruimer dan een
        // gemiddelde kleine letter, zodat we eerder te vroeg dan te laat
        // afbreken.
        _ => 0.58,
    }
}

/// Row data (pre-computed heights with text wrapping).
#[derive(Debug, Clone)]
struct RowLayout {
    /// Original cell texts.
    cells: Vec<String>,
    /// Wrapped lines per cell (computed during wrap phase).
    wrapped_cells: Vec<Vec<String>>,
    height: Pt,
    is_header: bool,
}

/// Table flowable.
#[derive(Debug)]
pub struct Table {
    headers: Vec<String>,
    rows: Vec<Vec<String>>,
    col_widths: Option<Vec<Pt>>,
    style: TableStyleConfig,
    repeat_header: bool,
    /// Per-datarij stijl-afwijkingen (zelfde index als `rows`).
    row_overrides: Vec<Option<RowOverride>>,
    /// Uitlijning per kolom (default: links). Rechts voor getalkolommen.
    col_alignments: Vec<Alignment>,
    // Computed after wrap()
    computed_col_widths: Vec<Pt>,
    row_layouts: Vec<RowLayout>,
    wrapped_height: Pt,
}

impl Table {
    pub fn new(headers: Vec<String>, rows: Vec<Vec<String>>) -> Self {
        Self {
            headers,
            rows,
            col_widths: None,
            style: TableStyleConfig::default(),
            repeat_header: true,
            row_overrides: Vec::new(),
            col_alignments: Vec::new(),
            computed_col_widths: Vec::new(),
            row_layouts: Vec::new(),
            wrapped_height: Pt::ZERO,
        }
    }

    /// Per-datarij stijl-afwijkingen; index loopt gelijk met de datarijen.
    pub fn with_row_overrides(mut self, overrides: Vec<Option<RowOverride>>) -> Self {
        self.row_overrides = overrides;
        self
    }

    /// Uitlijning per kolom (links is de default); rechts voor getallen.
    pub fn with_col_alignments(mut self, alignments: Vec<Alignment>) -> Self {
        self.col_alignments = alignments;
        self
    }

    pub fn with_col_widths(mut self, widths: Vec<Pt>) -> Self {
        self.col_widths = Some(widths);
        self
    }

    pub fn with_col_widths_mm(mut self, widths_mm: Vec<f64>) -> Self {
        self.col_widths = Some(
            widths_mm
                .into_iter()
                .map(|w| crate::types::Mm(w as f32).into())
                .collect(),
        );
        self
    }

    pub fn with_style(mut self, style: TableStyleConfig) -> Self {
        self.style = style;
        self
    }

    pub fn with_repeat_header(mut self, repeat: bool) -> Self {
        self.repeat_header = repeat;
        self
    }

    /// Calculate column widths (equal distribution if not specified).
    fn compute_col_widths(&self, available_width: Pt) -> Vec<Pt> {
        if let Some(ref widths) = self.col_widths {
            return widths.clone();
        }

        let num_cols = self.headers.len().max(
            self.rows
                .first()
                .map(|r| r.len())
                .unwrap_or(self.headers.len()),
        );
        if num_cols == 0 {
            return Vec::new();
        }

        let col_width = Pt(available_width.0 / num_cols as f32);
        vec![col_width; num_cols]
    }

    /// Single-line row height (used for comparison in tests).
    #[allow(dead_code)]
    fn single_row_height(&self, font_size: Pt) -> Pt {
        let padding = self.style.cell_padding.vertical();
        Pt(font_size.0 + padding.0 + 2.0)
    }

    /// Wrap cell text to fit within column width.
    ///
    /// Meet de tekst per teken in plaats van met één vaste breedte. Een vlakke
    /// 0.5 × font_size onderschat hoofdletters fors — in een proportioneel
    /// font is een 'M' ruim twee keer zo breed als een 'i' — waardoor een
    /// hoofdstuktitel in kapitalen niet omsloeg maar over de buurkolom heen
    /// werd getekend.
    fn wrap_text(&self, text: &str, col_width: Pt, font_size: Pt, bold: bool) -> Vec<String> {
        let usable_width = col_width.0
            - self.style.cell_padding.left.0
            - self.style.cell_padding.right.0;

        if usable_width <= 0.0 || text.is_empty() {
            return vec![text.to_string()];
        }

        // Vet is bij Liberation/DejaVu enkele procenten breder dan regulier.
        let bold_factor = if bold { 1.06 } else { 1.0 };
        let width_of = |s: &str| -> f32 {
            s.chars().map(char_em_width).sum::<f32>() * font_size.0 * bold_factor
        };

        if width_of(text) <= usable_width {
            return vec![text.to_string()];
        }

        // Inspring van de eerste regel (bv. hiërarchie-indent) ook op de
        // vervolgregels toepassen, zodat een omgeslagen omschrijving in de
        // eigen kolom-inspringing blijft hangen.
        let indent: String = text.chars().take_while(|c| *c == ' ').collect();

        let mut lines = Vec::new();
        let mut remaining = text;

        while !remaining.is_empty() {
            if width_of(remaining) <= usable_width {
                lines.push(remaining.to_string());
                break;
            }

            // Loop mee tot de breedte vol is en onthoud de byte-index van dat
            // teken. Op bytes slicen paniekt midden in een multi-byte teken
            // ('×', 'm²', 'm¹'), dus altijd via char_indices.
            let mut breedte = 0.0f32;
            let mut hard_end = remaining.len();
            for (idx, ch) in remaining.char_indices() {
                breedte += char_em_width(ch) * font_size.0 * bold_factor;
                if breedte > usable_width {
                    // Minstens één teken per regel, anders loopt dit vast.
                    hard_end = if idx == 0 {
                        remaining
                            .char_indices()
                            .nth(1)
                            .map(|(i, _)| i)
                            .unwrap_or(remaining.len())
                    } else {
                        idx
                    };
                    break;
                }
            }

            // Terug naar de laatste spatie die nog binnen de breedte viel
            let split_at = remaining[..hard_end]
                .rfind(' ')
                .map(|pos| pos + 1)
                .unwrap_or(hard_end);

            lines.push(remaining[..split_at].trim_end().to_string());
            remaining = remaining[split_at..].trim_start();
        }

        if lines.is_empty() {
            lines.push(String::new());
        }
        if !indent.is_empty() {
            for line in lines.iter_mut().skip(1) {
                *line = format!("{indent}{line}");
            }
        }
        lines
    }

    /// Compute wrapped row layout: wrap all cells and calculate row height.
    fn compute_row_layout(
        &self,
        cells: &[String],
        is_header: bool,
        bold: bool,
    ) -> RowLayout {
        let font_size = if is_header {
            self.style.header_font_size
        } else {
            self.style.font_size
        };

        let wrapped_cells: Vec<Vec<String>> = cells
            .iter()
            .enumerate()
            .map(|(col_idx, text)| {
                let col_width = self
                    .computed_col_widths
                    .get(col_idx)
                    .copied()
                    .unwrap_or(Pt(50.0));
                self.wrap_text(text, col_width, font_size, bold)
            })
            .collect();

        // Row height = max lines across all cells × leading + padding
        let max_lines = wrapped_cells.iter().map(|w| w.len()).max().unwrap_or(1);
        let leading = font_size.0 * 1.2;
        let padding = self.style.cell_padding.vertical();
        let height = Pt(max_lines as f32 * leading + padding.0 + 2.0);

        RowLayout {
            cells: cells.to_vec(),
            wrapped_cells,
            height,
            is_header,
        }
    }

    /// Draw a single row with multi-line text wrapping support.
    fn draw_row(
        &self,
        row: &RowLayout,
        x: Pt,
        y: Pt,
        draw_list: &mut DrawList,
        row_index: usize,
    ) {
        let bg_color = if row.is_header {
            self.style.header_background
        } else {
            let pattern_idx = row_index % self.style.row_backgrounds.len().max(1);
            self.style
                .row_backgrounds
                .get(pattern_idx)
                .cloned()
                .flatten()
        };

        // Draw row background
        if let Some(bg) = bg_color {
            draw_list.set_fill_color(bg);
            let total_width: f32 = self.computed_col_widths.iter().map(|w| w.0).sum();
            draw_list.draw_rect(x, y, Pt(total_width), row.height, true, false);
        }

        // Draw cell text
        let text_color = if row.is_header {
            self.style.header_text_color
        } else {
            Color::BLACK
        };

        let font_size = if row.is_header {
            self.style.header_font_size
        } else {
            self.style.font_size
        };

        let font_name = if row.is_header {
            self.style.header_font_name.clone()
        } else {
            self.row_overrides
                .get(row_index)
                .and_then(|o| o.as_ref())
                .and_then(|o| o.font_name.clone())
                .unwrap_or_else(|| self.style.font_name.clone())
        };

        draw_list.set_font(&font_name, font_size);
        draw_list.set_fill_color(text_color);

        let leading = font_size.0 * 1.2;
        let base_text_y = Pt(y.0 + self.style.cell_padding.top.0 + font_size.0 * 0.8);
        let mut cx = x;

        for (col_idx, wrapped_lines) in row.wrapped_cells.iter().enumerate() {
            let col_width = self
                .computed_col_widths
                .get(col_idx)
                .copied()
                .unwrap_or(Pt(50.0));

            let align = self
                .col_alignments
                .get(col_idx)
                .copied()
                .unwrap_or(Alignment::Left);

            // Draw each wrapped line
            for (line_idx, line) in wrapped_lines.iter().enumerate() {
                let line_y = Pt(base_text_y.0 + line_idx as f32 * leading);
                match align {
                    Alignment::Right => {
                        let right_x = Pt(cx.0 + col_width.0 - self.style.cell_padding.right.0);
                        draw_list.draw_text_right(right_x, line_y, line);
                    }
                    _ => {
                        let text_x = Pt(cx.0 + self.style.cell_padding.left.0);
                        draw_list.draw_text(text_x, line_y, line);
                    }
                }
            }

            cx = Pt(cx.0 + col_width.0);
        }

        // Draw grid lines (clean stijl: grid_width <= 0 → geen cellijnen)
        if self.style.grid_width.0 > 0.0 {
            draw_list.set_stroke_color(self.style.grid_color);
            draw_list.set_line_width(self.style.grid_width);

            let total_width: f32 = self.computed_col_widths.iter().map(|w| w.0).sum();
            // Bottom line
            draw_list.draw_line(
                x,
                Pt(y.0 + row.height.0),
                Pt(x.0 + total_width),
                Pt(y.0 + row.height.0),
            );

            // Vertical lines
            let mut vx = x;
            for col_width in &self.computed_col_widths {
                draw_list.draw_line(vx, y, vx, Pt(y.0 + row.height.0));
                vx = Pt(vx.0 + col_width.0);
            }
            // Right edge
            draw_list.draw_line(vx, y, vx, Pt(y.0 + row.height.0));
        } else if row.is_header && self.style.header_rule {
            // Alleen een dunne lijn onder de koprij
            draw_list.set_stroke_color(self.style.grid_color);
            draw_list.set_line_width(Pt(0.75));
            let total_width: f32 = self.computed_col_widths.iter().map(|w| w.0).sum();
            draw_list.draw_line(
                x,
                Pt(y.0 + row.height.0),
                Pt(x.0 + total_width),
                Pt(y.0 + row.height.0),
            );
        }

        // Per-rij lijnen uit de RowOverride (clean stijl: hoofdstukregels)
        if !row.is_header {
            if let Some(ov) = self.row_overrides.get(row_index).and_then(|o| o.as_ref()) {
                if ov.top_rule || ov.bottom_rule {
                    draw_list.set_stroke_color(self.style.grid_color);
                    draw_list.set_line_width(Pt(1.1));
                    let total_width: f32 = self.computed_col_widths.iter().map(|w| w.0).sum();
                    if ov.top_rule {
                        draw_list.draw_line(x, y, Pt(x.0 + total_width), y);
                    }
                    if ov.bottom_rule {
                        draw_list.draw_line(
                            x,
                            Pt(y.0 + row.height.0),
                            Pt(x.0 + total_width),
                            Pt(y.0 + row.height.0),
                        );
                    }
                }
                if ov.top_rule_sum {
                    // Som-lijn (subtotaal): zwart, dikker, ~5 mm rechts
                    // doorgetrokken met een '+' boven het uiteinde.
                    let total_width: f32 = self.computed_col_widths.iter().map(|w| w.0).sum();
                    let extend: Pt = crate::types::Mm(5.0).into();
                    let end_x = Pt(x.0 + total_width + extend.0);
                    draw_list.set_stroke_color(Color::rgb(26, 26, 26));
                    draw_list.set_line_width(Pt(1.4));
                    draw_list.draw_line(x, y, end_x, y);
                    draw_list.set_font(&self.style.font_name, Pt(8.0));
                    draw_list.set_fill_color(Color::rgb(26, 26, 26));
                    draw_list.draw_text(Pt(end_x.0 - 5.0), Pt(y.0 - 2.0), "+");
                }
            }
        }
    }
}

impl Flowable for Table {
    fn wrap(&mut self, available_width: Pt, _available_height: Pt, _ctx: &LayoutContext) -> Size {
        self.computed_col_widths = self.compute_col_widths(available_width);

        self.row_layouts.clear();

        // Header row — koppen staan vet, dus ook breder meten.
        if !self.headers.is_empty() {
            self.row_layouts
                .push(self.compute_row_layout(&self.headers.clone(), true, true));
        }

        // Data rows — een rij met een vet lettertype (hoofdstuktitels) is
        // breder dan de reguliere schatting; die vlag gaat mee de terugloop in.
        for (i, row) in self.rows.clone().into_iter().enumerate() {
            let bold = self
                .row_overrides
                .get(i)
                .and_then(|o| o.as_ref())
                .and_then(|o| o.font_name.as_deref())
                .map(|f| f.contains("Bold"))
                .unwrap_or(false);
            self.row_layouts
                .push(self.compute_row_layout(&row, false, bold));
        }

        self.wrapped_height = Pt(self.row_layouts.iter().map(|r| r.height.0).sum());

        Size::new(available_width, self.wrapped_height)
    }

    fn draw(&self, x: Pt, y: Pt, draw_list: &mut DrawList) {
        // Top border (niet in clean stijl)
        if self.style.grid_width.0 > 0.0 {
            let total_width: f32 = self.computed_col_widths.iter().map(|w| w.0).sum();
            draw_list.set_stroke_color(self.style.grid_color);
            draw_list.set_line_width(self.style.grid_width);
            draw_list.draw_line(x, y, Pt(x.0 + total_width), y);
        }

        let mut cy = y;
        let mut data_row_idx = 0;

        for row in &self.row_layouts {
            self.draw_row(row, x, cy, draw_list, data_row_idx);
            cy = Pt(cy.0 + row.height.0);
            if !row.is_header {
                data_row_idx += 1;
            }
        }
    }

    fn split(
        &self,
        available_width: Pt,
        available_height: Pt,
        ctx: &LayoutContext,
    ) -> SplitResult {
        if self.wrapped_height.0 <= available_height.0 {
            return SplitResult::Fits;
        }

        // Find how many rows fit
        let mut used_height = 0.0_f32;
        let mut split_at = 0;

        for (i, row) in self.row_layouts.iter().enumerate() {
            if used_height + row.height.0 > available_height.0 {
                split_at = i;
                break;
            }
            used_height += row.height.0;
            split_at = i + 1;
        }

        // Need at least header + 1 data row
        let min_rows = if self.headers.is_empty() { 1 } else { 2 };
        if split_at < min_rows {
            return SplitResult::CannotSplit;
        }

        // Split rows
        let first_data_rows: Vec<Vec<String>> = self.row_layouts[..split_at]
            .iter()
            .filter(|r| !r.is_header)
            .map(|r| r.cells.clone())
            .collect();

        let second_data_rows: Vec<Vec<String>> = self.row_layouts[split_at..]
            .iter()
            .filter(|r| !r.is_header)
            .map(|r| r.cells.clone())
            .collect();

        // Verdeel de per-rij overrides over beide tafel-helften (zelfde
        // datarij-indexering als de rows zelf).
        let n_first = first_data_rows.len();
        let (ov_first, ov_second): (Vec<Option<RowOverride>>, Vec<Option<RowOverride>>) =
            if self.row_overrides.is_empty() {
                (Vec::new(), Vec::new())
            } else {
                let mut padded = self.row_overrides.clone();
                padded.resize(self.rows.len(), None);
                let second = padded.split_off(n_first.min(padded.len()));
                (padded, second)
            };

        let mut first = Table::new(self.headers.clone(), first_data_rows)
            .with_style(self.style.clone())
            .with_row_overrides(ov_first)
            .with_col_alignments(self.col_alignments.clone());
        if let Some(ref widths) = self.col_widths {
            first = first.with_col_widths(widths.clone());
        }

        let mut second = Table::new(
            if self.repeat_header {
                self.headers.clone()
            } else {
                Vec::new()
            },
            second_data_rows,
        )
        .with_style(self.style.clone())
        .with_row_overrides(ov_second)
        .with_col_alignments(self.col_alignments.clone());
        if let Some(ref widths) = self.col_widths {
            second = second.with_col_widths(widths.clone());
        }

        first.wrap(available_width, Pt(f32::MAX), ctx);
        second.wrap(available_width, Pt(f32::MAX), ctx);

        SplitResult::Split(Box::new(first), Box::new(second))
    }

    fn height(&self) -> Pt {
        self.wrapped_height
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fonts::shared_font_registry;

    fn test_ctx() -> LayoutContext {
        LayoutContext {
            fonts: shared_font_registry(),
        }
    }

    #[test]
    fn test_table_wrap() {
        let mut table = Table::new(
            vec!["A".into(), "B".into(), "C".into()],
            vec![
                vec!["1".into(), "2".into(), "3".into()],
                vec!["4".into(), "5".into(), "6".into()],
            ],
        );

        let ctx = test_ctx();
        let size = table.wrap(Pt(500.0), Pt(1000.0), &ctx);

        assert!(size.height.0 > 0.0);
        assert_eq!(table.row_layouts.len(), 3); // 1 header + 2 data
    }

    #[test]
    fn test_equal_col_widths() {
        let table = Table::new(
            vec!["A".into(), "B".into()],
            vec![vec!["1".into(), "2".into()]],
        );

        let widths = table.compute_col_widths(Pt(200.0));
        assert_eq!(widths.len(), 2);
        assert!((widths[0].0 - 100.0).abs() < 0.01);
    }

    #[test]
    fn test_text_wrapping() {
        let mut table = Table::new(
            vec!["Header".into()],
            vec![vec![
                "This is a very long text that should definitely wrap across multiple lines in the table cell"
                    .into(),
            ]],
        )
        .with_col_widths(vec![Pt(80.0)]); // Narrow column

        let ctx = test_ctx();
        let _size = table.wrap(Pt(80.0), Pt(1000.0), &ctx);

        // Row with wrapping should be taller than single-line row
        assert!(table.row_layouts.len() == 2); // header + 1 data row
        let data_row = &table.row_layouts[1];
        assert!(
            data_row.wrapped_cells[0].len() > 1,
            "Long text should wrap: {:?}",
            data_row.wrapped_cells[0]
        );
        // Height should reflect multiple lines
        let single_h = table.single_row_height(table.style.font_size);
        assert!(
            data_row.height.0 > single_h.0,
            "Wrapped row should be taller than single-line: {} vs {}",
            data_row.height.0,
            single_h.0
        );
    }

    #[test]
    fn test_short_text_no_wrap() {
        let mut table = Table::new(
            vec!["A".into(), "B".into()],
            vec![vec!["Short".into(), "Text".into()]],
        );

        let ctx = test_ctx();
        table.wrap(Pt(500.0), Pt(1000.0), &ctx);

        let data_row = &table.row_layouts[1];
        // Short text should not wrap
        assert_eq!(data_row.wrapped_cells[0].len(), 1);
        assert_eq!(data_row.wrapped_cells[1].len(), 1);
    }
}
