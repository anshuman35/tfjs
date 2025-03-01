/**
 * @license
 * Copyright 2023 Google LLC.
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 * =============================================================================
 */

/**
 *  Base class for Backbone models.
 */

/* Original source: keras_nlp/models/backbone.py */
import { serialization } from '@tensorflow/tfjs-core';

import { ContainerArgs } from '../../../engine/container';
import { LayersModel } from '../../../engine/training';
import { NotImplementedError } from '../../../errors';
import { Layer } from '../../../exports_layers';

export class Backbone extends LayersModel {
  /** @nocollapse */
  static override className = 'Backbone';

  constructor(args: ContainerArgs) {
    super(args);
  }

  /**
   * A `tf.layers.embedding` instance for embedding token ids.
   */
  get tokenEmbedding(): Layer {
    throw new NotImplementedError();
  }

  override getConfig(): serialization.ConfigDict {
    return {
      name: this.name,
      trainable: this.trainable,
    };
  }

  static override fromConfig<T extends serialization.Serializable>(
    cls: serialization.SerializableConstructor<T>,
    config: serialization.ConfigDict): T {

    return new cls(config);
  }
}
serialization.registerClass(Backbone);
