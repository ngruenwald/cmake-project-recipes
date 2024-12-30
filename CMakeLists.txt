cmake_minimum_required(VERSION 3.20)

add_custom_target(cmake_project_recipes)

set_target_properties(
  cmake_project_recipes
  PROPERTIES
    RECIPES_PATH  "${CMAKE_CURRENT_LIST_DIR}/recipes"
)
