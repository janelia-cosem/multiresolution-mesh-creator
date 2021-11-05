import trimesh
from trimesh.intersections import slice_faces_plane
import numpy as np
from dvidutils import encode_faces_to_custom_drc_bytes
import time
import os
from os import listdir
from os.path import isfile, join, splitext
import dask
from dask.distributed import worker_client
import pyfqmr
import multiresolution_mesh_creator.util.mesh_util as mesh_util
import multiresolution_mesh_creator.util.io_util as io_util
import multiresolution_mesh_creator.util.dask_util as dask_util
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


def my_slice_faces_plane(vertices, faces, plane_normal, plane_origin):
    """Wrapper for trimesh slice_faces_plane to catch error that happens if the
    whole mesh is to one side of the plane.

    Args:
        vertices: Mesh vertices
        faces: Mesh faces
        plane_normal: Normal of plane
        plane_origin: Origin of plane

    Returns:
        vertices, faces: Vertices and faces
    """

    if len(vertices) > 0 and len(faces) > 0:
        try:
            vertices, faces = slice_faces_plane(vertices, faces, plane_normal,
                                                plane_origin)
        except ValueError as e:
            if str(e) != "input must be 1D integers!":
                raise
            else:
                pass

    return vertices, faces


def update_fragment_dict(dictionary, fragment_pos, vertices, faces,
                         lod_0_fragment_pos):
    """Update dictionary (in place) whose keys are fragment positions and
    whose values are `Fragment` which is a class containing the corresponding
    fragment vertices, faces and corresponding lod 0 fragment positions.

    This is necessary since each fragment (above lod 0) must be divisible by a
    2x2x2 grid. So each fragment is technically split into many "subfragments".
    Thus the dictionary is used to map all subfragments to the proper parent
    fragment. The lod 0 fragment positions are used when writing out the index
    files because if a subfragment is empty it still needs to be included in
    the index file. By tracking all the corresponding lod 0 fragments of a
    given lod fragment, we can ensure that all necessary empty fragments are
    included.

    Args:
        dictionary: Dictionary of fragment pos keys and fragment info values
        fragment_pos: Current lod fragment position
        vertices: Vertices
        faces: Faces
        lod_0_fragment_pos: Corresponding lod 0 fragment positions
                            corresponding to fragment_pos
    """

    if fragment_pos in dictionary:
        fragment = dictionary[fragment_pos]
        fragment.update(vertices, faces, lod_0_fragment_pos)
        dictionary[fragment_pos] = fragment
    else:
        dictionary[fragment_pos] = mesh_util.Fragment(vertices, faces,
                                                      [lod_0_fragment_pos])


@dask.delayed
def generate_mesh_decomposition(vertices, faces, lod_0_box_size,
                                start_fragment, end_fragment, current_lod,
                                num_chunks):
    """Dask delayed function to decompose a mesh, provided as vertices and
    faces, into fragments of size lod_0_box_size * 2**current_lod. Each
    fragment is also subdivided by 2x2x2. This is performed over a limited
    xrange in order to parallelize via dask.

    Args:
        vertices: Vertices
        faces: Faces
        lod_0_box_size: Base chunk shape
        start_fragment: Start fragment position (x,y,z)
        end_fragment: End fragment position (x,y,z)
        x_start: Starting x position for this dask task
        x_end: Ending x position for this dask task
        current_lod: The current level of detail

    Returns:
        fragments: List of `CompressedFragments` (named tuple)
    """
    vertices = vertices[0][1]
    faces = faces[0][1]
    combined_fragments_dictionary = {}
    fragments = []

    nyz, nxz, nxy = np.eye(3)

    if current_lod != 0:
        # Want each chunk for lod>0 to be divisible by 2x2x2 region,
        # so multiply coordinates by 2
        start_fragment *= 2
        end_fragment *= 2

        # 2x2x2 subdividing box size
        sub_box_size = lod_0_box_size * 2**(current_lod - 1)
    else:
        sub_box_size = lod_0_box_size

    # Set up slab for current dask task
    n = np.eye(3)
    for dimension in range(3):
        if num_chunks[dimension] > 1:
            n_d = n[dimension, :]
            plane_origin = n_d * end_fragment[dimension] * sub_box_size
            vertices, faces = my_slice_faces_plane(vertices, faces, -n_d,
                                                   plane_origin)
            if len(vertices) == 0:
                return None
            plane_origin = n_d * start_fragment[dimension] * sub_box_size
            vertices, faces = my_slice_faces_plane(vertices, faces, n_d,
                                                   plane_origin)

    if len(vertices) == 0:
        return None

    for x in range(start_fragment[0], end_fragment[0]):
        plane_origin_yz = nyz * (x + 1) * sub_box_size
        vertices_yz, faces_yz = my_slice_faces_plane(vertices, faces, -nyz,
                                                     plane_origin_yz)

        for y in range(start_fragment[1], end_fragment[1]):
            plane_origin_xz = nxz * (y + 1) * sub_box_size
            vertices_xz, faces_xz = my_slice_faces_plane(
                vertices_yz, faces_yz, -nxz, plane_origin_xz)

            for z in range(start_fragment[2], end_fragment[2]):
                plane_origin_xy = nxy * (z + 1) * sub_box_size
                vertices_xy, faces_xy = my_slice_faces_plane(
                    vertices_xz, faces_xz, -nxy, plane_origin_xy)

                lod_0_fragment_position = tuple(np.array([x, y, z]))
                if current_lod != 0:
                    fragment_position = tuple(np.array([x, y, z]) // 2)
                else:
                    fragment_position = lod_0_fragment_position

                update_fragment_dict(combined_fragments_dictionary,
                                     fragment_position, vertices_xy, faces_xy,
                                     list(lod_0_fragment_position))

                vertices_xz, faces_xz = my_slice_faces_plane(
                    vertices_xz, faces_xz, nxy, plane_origin_xy)

            vertices_yz, faces_yz = my_slice_faces_plane(
                vertices_yz, faces_yz, nxz, plane_origin_xz)

        vertices, faces = my_slice_faces_plane(vertices, faces, nyz,
                                               plane_origin_yz)

    # return combined_fragments_dictionary
    for fragment_pos, fragment in combined_fragments_dictionary.items():
        current_box_size = lod_0_box_size * 2**current_lod
        draco_bytes = encode_faces_to_custom_drc_bytes(
            fragment.vertices,
            np.zeros(np.shape(fragment.vertices)),
            fragment.faces,
            np.asarray(3 * [current_box_size]),
            np.asarray(fragment_pos) * current_box_size,
            position_quantization_bits=10)

        if len(draco_bytes) > 12:
            fragment = mesh_util.CompressedFragment(
                draco_bytes, np.asarray(fragment_pos), len(draco_bytes),
                np.asarray(fragment.lod_0_fragment_pos))
            fragments.append(fragment)

    return fragments


@dask.delayed
def pyfqmr_decimate(input_path, output_path, id, lod, ext, decimation_factor,
                    aggressiveness):
    """Mesh decimation using pyfqmr.

    Decimation is performed on a mesh located at `input_path`/`id`.`ext`. The
    target number of faces is 1/2**`lod` of the current number of faces. The
    mesh is written to an stl file in `output_path`/s`lod`/`id`.stl. This
    utilizes `dask.delayed`.

    Args:
        input_path [`str`]: The input path for s0 meshes
        output_path [`str`]: The output path
        id [`int`]: The object id
        lod [`int`]: The current level of detail
        ext [`str`]: The extension of the s0 meshes.
        decimation_factor [`float`]: The factor by which we decimate faces,
                                     scaled by 2**lod
        aggressiveness [`int`]: Aggressiveness for decimation
    """

    vertices, faces = mesh_util.mesh_loader(f"{input_path}/{id}{ext}")
    desired_faces = max(len(faces) // (decimation_factor**lod), 4)
    mesh_simplifier = pyfqmr.Simplify()
    mesh_simplifier.setMesh(vertices, faces)
    mesh_simplifier.simplify_mesh(target_count=desired_faces,
                                  aggressiveness=aggressiveness,
                                  preserve_border=False,
                                  verbose=False)
    vertices, faces, _ = mesh_simplifier.getMesh()

    mesh = trimesh.Trimesh(vertices, faces)
    mesh.export(f"{output_path}/s{lod}/{id}.stl")


def generate_decimated_meshes(input_path, output_path, lods, ids, ext,
                              decimation_factor, aggressiveness):
    """Generate decimatated meshes for all ids in `ids`, over all lod in `lods`.

    Args:
        input_path (`str`): Input mesh paths
        output_path (`str`): Output mesh paths
        lods (`int`): Levels of detail over which to have mesh
        ids (`list`): All mesh ids
        ext (`str`): Input mesh formats.
        decimation_fraction [`float`]: The factor by which we decimate faces,
                                       scaled by 2**lod
        aggressiveness [`int`]: Aggressiveness for decimation
    """

    results = []
    for current_lod in lods:
        if current_lod == 0:
            os.makedirs(f"{output_path}/mesh_lods/", exist_ok=True)
            # link existing to s0
            if not os.path.exists(f"{output_path}/mesh_lods/s0"):
                os.system(
                    f"ln -s {os.path.abspath(input_path)}/ {os.path.abspath(output_path)}/mesh_lods/s0"
                )
        else:
            os.makedirs(f"{output_path}/mesh_lods/s{current_lod}",
                        exist_ok=True)
            for id in ids:
                results.append(
                    pyfqmr_decimate(input_path, f"{output_path}/mesh_lods", id,
                                    current_lod, ext, decimation_factor,
                                    aggressiveness))

    dask.compute(*results)


@dask.delayed
def generate_neuroglancer_multires_mesh(output_path, num_workers, id, lods,
                                        original_ext, lod_0_box_size):
    """Dask delayed function to generate multiresolution mesh in neuroglancer
    mesh format using prewritten meshes at different levels of detail.

    This function generates the neuroglancer mesh for a single mesh, and
    parallelizes the mesh creation over `num_workers` by splitting the mesh in
    the x-direciton into `num_workers` fragments, each of which is sent to a
    a worker to be further subdivided.

    Args:
        output_path (`str`): Output path to write out neuroglancer mesh
        num_workers (`int`): Number of workers for dask
        id (`int`): Mesh id
        lods (`list`): List of levels of detail
        original_ext (`str`): Original mesh file extension
        lod_0_box_size (`int`): Box size in lod 0 coordinates
    """

    with worker_client() as client:
        os.makedirs(f"{output_path}/multires", exist_ok=True)
        os.system(
            f"rm -rf {output_path}/multires/{id} {output_path}/multires/{id}.index"
        )

        results = []

        for idx, current_lod in enumerate(lods):
            if current_lod == 0:
                vertices, faces = mesh_util.mesh_loader(
                    f"{output_path}/mesh_lods/s{current_lod}/{id}{original_ext}"
                )
            else:
                vertices, faces = mesh_util.mesh_loader(
                    f"{output_path}/mesh_lods/s{current_lod}/{id}.stl")

            current_box_size = lod_0_box_size * 2**current_lod
            start_fragment = np.maximum(
                vertices.min(axis=0) // current_box_size - 1,
                np.array([0, 0, 0])).astype(int)
            end_fragment = (vertices.max(axis=0) // current_box_size +
                            1).astype(int)

            # if it can all fit in one dimension, do that
            # if its been filled up and can add to next dimension, do that
            # etc

            max_number_of_chunks = (end_fragment - start_fragment)
            dimensions_sorted = np.argsort(-max_number_of_chunks)
            num_chunks = np.array([1, 1, 1])
            for _ in range(num_workers + 1):
                for d in dimensions_sorted:
                    if num_chunks[d] < max_number_of_chunks[d]:
                        num_chunks[d] += 1
                        if np.prod(num_chunks) > num_workers:
                            num_chunks[d] -= 1
                        break

            stride = np.ceil(1.0 * (end_fragment - start_fragment) /
                             num_chunks).astype(np.int)

            unique_name_vertices = f"vertices_{id}_{current_lod}_{datetime.now()}"
            unique_name_faces = f"faces_{id}_{current_lod}_{datetime.now()}"
            # if vertices.nbytes + faces.nbytes > 100_000:
            # scatter large ones
            # set hash to false here in attempt to prevent this issue: https://github.com/dask/distributed/issues/4612 ?
            vertices_to_send = client.scatter(
                [[unique_name_vertices, vertices]], broadcast=True)
            faces_to_send = client.scatter([[unique_name_faces, faces]],
                                           broadcast=True)
            # else:
            #    vertices_to_send = vertices
            #    faces_to_send = faces

            for x in range(start_fragment[0], end_fragment[0], stride[0]):
                for y in range(start_fragment[1], end_fragment[1], stride[1]):
                    for z in range(start_fragment[2], end_fragment[2],
                                   stride[2]):
                        current_start_fragment = np.array([x, y, z])
                        current_end_fragment = current_start_fragment + stride
                        #unique_name_function = f"{unique_name_vertices}_{unique_name_faces}_{x}_{y}_{z}_{datetime.now()}"
                        results.append(
                            generate_mesh_decomposition(
                                vertices_to_send, faces_to_send,
                                lod_0_box_size, current_start_fragment,
                                current_end_fragment, current_lod, num_chunks))
            client.rebalance()
            dask_results = dask.compute(*results)

            # Remove empty slabs
            dask_results = [
                fragments for fragments in dask_results if fragments
            ]

            fragments = [
                fragment for fragments in dask_results
                for fragment in fragments
            ]

            results = []
            dask_results = []
            mesh_util.write_mesh_files(
                f"{output_path}/multires", f"{id}", fragments, current_lod,
                lods[:idx + 1],
                np.asarray([lod_0_box_size, lod_0_box_size, lod_0_box_size]))

    io_util.print_with_datetime(
        f"Completed creation of multiresolution neuroglancer mesh for mesh {id}!",
        logger)


def generate_all_neuroglancer_multires_meshes(output_path, num_workers, ids,
                                              lods, original_ext,
                                              lod_0_box_size):
    """Generate all neuroglancer multiresolution meshes for `ids`. Calls dask
    delayed function `generate_neuroglancer_multires_mesh` for each id.

    Args:
        output_path (`str`): Output path to write out neuroglancer mesh
        num_workers (`int`): Number of workers for dask
        ids (`list`): List of mesh ids
        lods (`list`): List of levels of detail
        original_ext (`str`): Original mesh file extension
        lod_0_box_size (`int`): Box size in lod 0 coordinates
    """

    results = []
    for id in ids:
        results.append(
            generate_neuroglancer_multires_mesh(output_path, num_workers, id,
                                                lods, original_ext,
                                                lod_0_box_size))
    dask.compute(*results)


def main():
    submission_directory = os.getcwd()

    # If more than 1 thread per worker, run into issues with decimation?
    args = io_util.parser_params()
    num_workers = args.num_workers
    required_settings, optional_decimation_settings = io_util.read_run_config(
        args.config_path)

    input_path = required_settings['input_path']
    output_path = required_settings['output_path']
    num_lods = required_settings['num_lods']
    lod_0_box_size = required_settings['box_size']

    skip_decimation = optional_decimation_settings['skip_decimation']
    decimation_factor = optional_decimation_settings['decimation_factor']
    aggressiveness = optional_decimation_settings['aggressiveness']

    execution_directory = dask_util.setup_execution_directory(
        args.config_path, logger)
    logpath = f'{execution_directory}/output.log'

    with io_util.tee_streams(logpath):

        try:
            os.chdir(execution_directory)

            lods = list(range(num_lods))
            mesh_files = [
                f for f in listdir(input_path) if isfile(join(input_path, f))
            ]
            mesh_ids = [splitext(mesh_file)[0] for mesh_file in mesh_files]
            mesh_ext = splitext(mesh_files[0])[1]

            t0 = time.time()

            # Mesh decimation
            if not skip_decimation:
                # Start dask
                with dask_util.start_dask(num_workers, "decimation", logger):
                    with io_util.Timing_Messager("Generating decimated meshes",
                                                 logger):
                        generate_decimated_meshes(input_path, output_path,
                                                  lods, mesh_ids, mesh_ext,
                                                  decimation_factor,
                                                  aggressiveness)

            # Restart dask to clean up cluster before multires assembly
            with dask_util.start_dask(num_workers, "multires creation",
                                      logger):
                # Create multiresolution meshes
                with io_util.Timing_Messager("Generating multires meshes",
                                             logger):
                    generate_all_neuroglancer_multires_meshes(
                        output_path, num_workers, mesh_ids, lods, mesh_ext,
                        lod_0_box_size)

            # Writing out top-level files
            with io_util.Timing_Messager(
                    "Writing info and segment properties files", logger):
                output_path = f"{output_path}/multires"
                mesh_util.write_segment_properties_file(output_path)
                mesh_util.write_info_file(output_path)

            io_util.print_with_datetime(
                f"Complete! Elapsed time: {time.time() - t0}", logger)
        finally:
            os.chdir(submission_directory)


if __name__ == "__main__":
    main()