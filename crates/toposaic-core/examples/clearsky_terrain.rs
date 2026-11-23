//! Command-line bridge from aligned rasters to the TopoSaic print engine.

use std::{env, fs, io::BufReader, path::PathBuf};

use anyhow::{Context, Result, bail};
use serde::Deserialize;
use toposaic_core::{
    GenerationSpec, GroundColorMode, GroundImagery, GroundPaletteOptions, HeightField,
    SurfaceClass, SurfaceField, ThreeMfStyle, assign_locked_palette, discover_ground_palette,
    generate_project_with_fields,
};

#[derive(Deserialize)]
struct Job {
    spec: GenerationSpec,
    samples: usize,
    elevation_dimensions: [usize; 2],
    elevation_window: [usize; 4],
    elevation_source: String,
    imagery_source: String,
    elevations: Vec<f32>,
    palette_reference: Vec<[u16; 3]>,
    reference_valid: Vec<bool>,
    palette: Option<Vec<String>>,
    images: Vec<OutputImage>,
}

#[derive(Deserialize)]
struct OutputImage {
    name: String,
    pixels: Vec<[u16; 3]>,
    valid: Vec<bool>,
}

fn main() -> Result<()> {
    let args: Vec<_> = env::args_os().skip(1).collect();
    let [input, output] = args.as_slice() else {
        bail!("usage: clearsky_terrain JOB.json OUTPUT_DIR");
    };
    let input = PathBuf::from(input);
    let output = PathBuf::from(output);
    let job: Job = serde_json::from_reader(BufReader::new(fs::File::open(&input)?))
        .with_context(|| format!("read {}", input.display()))?;
    let size = job.samples;
    let [width, height] = job.elevation_dimensions;
    let height_field = HeightField::new(width, height, job.elevations, &job.elevation_source)?
        .with_sample_window(job.elevation_window)?;
    let colors = match job.palette {
        Some(colors) => colors,
        None => {
            let rgbn = expand(&job.palette_reference);
            let imagery = GroundImagery {
                width: size,
                height: size,
                rgbn: &rgbn,
                valid: &job.reference_valid,
            };
            let (palette, _) = discover_ground_palette(
                &imagery,
                None,
                &GroundPaletteOptions {
                    color_count: 4,
                    minimum_share: 0.02,
                    shadow_normalization: 0.0,
                },
            )?;
            palette
                .entries
                .into_iter()
                .map(|entry| entry.color)
                .collect()
        }
    };
    if !(1..=4).contains(&colors.len()) {
        bail!("the Creator 5 Pro needs one to four filament colors");
    }
    let mut spec = job.spec;
    spec.color_output.enabled = true;
    spec.color_output.threemf_style = ThreeMfStyle::ColorModel;
    // Reuse a palette color for the base and imagery gaps to avoid a fifth spool.
    spec.color_output.rock_color = colors[0].clone();
    spec.color_output.ground_palette.ground_colors = GroundColorMode::Satellite;
    spec.color_output.ground_palette.locked_ground_palette = Some(colors.clone());

    for image in job.images {
        if image.name.is_empty()
            || !image
                .name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-')
        {
            bail!("invalid image output name");
        }
        let rgbn = expand(&image.pixels);
        let imagery = GroundImagery {
            width: size,
            height: size,
            rgbn: &rgbn,
            valid: &image.valid,
        };
        let (palette, materials) = assign_locked_palette(&imagery, &colors, 0.0)?;
        let mut surface = SurfaceField::new(
            size,
            size,
            vec![SurfaceClass::Rock; size * size],
            format!("{} / {}", job.imagery_source, image.name),
        )?;
        surface.set_ground_palette(palette, materials)?;
        generate_project_with_fields(
            &spec,
            &height_field,
            Some(&surface),
            &output.join(image.name),
        )?;
    }
    println!("{}", serde_json::to_string(&colors)?);
    Ok(())
}

fn expand(pixels: &[[u16; 3]]) -> Vec<[u16; 4]> {
    pixels.iter().map(|&[r, g, b]| [r, g, b, 0]).collect()
}
